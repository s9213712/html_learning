#!/usr/bin/env python3
"""External remote-download worker for cloud drive tasks.

The Flask process owns authorization, quota checks, and final storage writes.
This worker owns the long-running network transfer and reports JSON lines to
stdout so the parent can update task progress without downloading in-process.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.storage.remote_downloads import (  # noqa: E402
    RemoteDownloadError,
    RemoteDownloadCancelled,
    RemoteDownloadPaused,
    download_remote_url,
    download_torrent_file_with_aria2,
    download_torrent_url_with_aria2,
)


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def _progress(event):
    _emit({"type": "progress", "event": event or {}})


def _positive_int_or_none(value):
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _control_check_from_file(path):
    control_path = Path(str(path or ""))
    if not str(path or "").strip():
        return None

    def _check():
        try:
            action = control_path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            action = ""
        if action == "pause":
            raise RemoteDownloadPaused("下載任務已暫停")
        if action == "cancel":
            raise RemoteDownloadCancelled("下載任務已取消")

    return _check


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run one cloud-drive remote download")
    parser.add_argument("--source-type", required=True, choices=["direct", "magnet", "torrent_url", "torrent_file"])
    parser.add_argument("--url", default="")
    parser.add_argument("--torrent-path", default="")
    parser.add_argument("--display-name", default="")
    parser.add_argument("--timeout-seconds", type=int, default=300)
    parser.add_argument("--max-bytes", type=int, default=0)
    parser.add_argument("--rate-limit-kb-per-sec", type=int, default=0)
    parser.add_argument("--control-file", default="")
    parser.add_argument("--owner-user-id", default="")
    parser.add_argument("--task-id", default="")
    args = parser.parse_args(argv)

    max_bytes = _positive_int_or_none(args.max_bytes)
    rate_limit = _positive_int_or_none(args.rate_limit_kb_per_sec)
    cancel_check = _control_check_from_file(args.control_file)

    try:
        if args.source_type == "torrent_file":
            downloaded = download_torrent_file_with_aria2(
                args.torrent_path,
                display_name=args.display_name or "BT 檔案",
                timeout_seconds=args.timeout_seconds,
                max_bytes=max_bytes,
                progress_callback=_progress,
                rate_limit_kb_per_sec=rate_limit,
                cancel_check=cancel_check,
                owner_user_id=args.owner_user_id,
                task_id=args.task_id,
            )
        elif args.source_type == "torrent_url":
            downloaded = download_torrent_url_with_aria2(
                args.url,
                timeout_seconds=args.timeout_seconds,
                max_bytes=max_bytes,
                progress_callback=_progress,
                rate_limit_kb_per_sec=rate_limit,
                cancel_check=cancel_check,
                owner_user_id=args.owner_user_id,
                task_id=args.task_id,
            )
        else:
            downloaded = download_remote_url(
                args.url,
                timeout_seconds=args.timeout_seconds,
                max_bytes=max_bytes,
                progress_callback=_progress,
                rate_limit_kb_per_sec=rate_limit,
                treat_torrent_as_bt=args.source_type != "direct",
                cancel_check=cancel_check,
                owner_user_id=args.owner_user_id,
                task_id=args.task_id,
            )
        _emit({
            "type": "result",
            "path": downloaded.path,
            "filename": downloaded.filename,
            "mimetype": downloaded.mimetype,
            "cleanup_dir": downloaded.cleanup_dir,
        })
        return 0
    except RemoteDownloadPaused as exc:
        _emit({"type": "paused", "message": str(exc)})
        return 3
    except RemoteDownloadCancelled as exc:
        _emit({"type": "cancelled", "message": str(exc)})
        return 4
    except RemoteDownloadError as exc:
        _emit({"type": "error", "message": str(exc)})
        return 2
    except Exception as exc:
        _emit({"type": "error", "message": f"遠端下載 worker 失敗：{exc.__class__.__name__}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
