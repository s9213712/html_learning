#!/usr/bin/env python3
"""Monitor one Transmission torrent and copy the finished payload."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

from services.storage.remote_downloads import _transmission_rpc_call


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: transmission_copy_monitor.py TORRENT_ID DEST_DIR", file=sys.stderr)
        return 64

    torrent_id = int(sys.argv[1])
    dest_dir = Path(sys.argv[2])
    deadline = time.time() + 21600
    fields = [
        "id",
        "name",
        "status",
        "percentDone",
        "totalSize",
        "downloadedEver",
        "rateDownload",
        "peersConnected",
        "eta",
        "error",
        "errorString",
        "downloadDir",
        "files",
    ]
    last = None
    print(f"monitor_start task={torrent_id} dest={dest_dir}", flush=True)
    source = None

    while time.time() < deadline:
        data = _transmission_rpc_call("torrent-get", {"ids": [torrent_id], "fields": fields})
        torrents = data.get("torrents") or []
        if not torrents:
            print(f"missing_torrent_{torrent_id}", flush=True)
            return 2

        torrent = torrents[0]
        pct = round(float(torrent.get("percentDone") or 0) * 100, 2)
        sig = (
            pct,
            torrent.get("downloadedEver"),
            torrent.get("rateDownload"),
            torrent.get("peersConnected"),
            torrent.get("eta"),
            torrent.get("errorString"),
        )
        if sig != last:
            print(
                json.dumps(
                    {
                        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "status": torrent.get("status"),
                        "percent": pct,
                        "downloaded": torrent.get("downloadedEver"),
                        "total": torrent.get("totalSize"),
                        "rate": torrent.get("rateDownload"),
                        "peers": torrent.get("peersConnected"),
                        "eta": torrent.get("eta"),
                        "error": torrent.get("errorString") or "",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            last = sig

        if torrent.get("error"):
            return 3
        if float(torrent.get("percentDone") or 0) >= 1:
            files = torrent.get("files") or []
            name = (files[0] or {}).get("name") if files else torrent.get("name")
            source = Path(torrent.get("downloadDir") or ".") / str(name)
            break
        time.sleep(30)
    else:
        print("monitor_timeout", flush=True)
        return 4

    if not source or not source.exists():
        print(f"source_missing {source}", flush=True)
        return 5

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(
        json.dumps(
            {"copy_start": str(source), "dest": str(dest), "size": source.stat().st_size},
            ensure_ascii=False,
        ),
        flush=True,
    )
    shutil.copy2(source, tmp)
    os.replace(tmp, dest)
    source_size = source.stat().st_size
    dest_size = dest.stat().st_size
    print(
        json.dumps(
            {"copy_done": str(dest), "source_size": source_size, "dest_size": dest_size, "ok": source_size == dest_size},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if source_size == dest_size else 6


if __name__ == "__main__":
    raise SystemExit(main())
