#!/usr/bin/env python3
import argparse
import json
import os
import subprocess
import time
import zipfile
from decimal import Decimal
from pathlib import Path

import requests


class Client:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({"Connection": "close"})
        self.csrf = ""

    def refresh_csrf(self):
        res = self.session.get(f"{self.base_url}/api/csrf-token", timeout=20)
        res.raise_for_status()
        self.csrf = res.json()["csrf_token"]
        return self.csrf

    def login(self):
        self.refresh_csrf()
        res = self.session.post(
            f"{self.base_url}/api/login",
            json={"username": self.username, "password": self.password},
            headers={"X-CSRF-Token": self.csrf},
            timeout=20,
        )
        self.refresh_csrf()
        return self.capture(res)

    def capture(self, res):
        try:
            body = res.json()
        except Exception:
            body = None
        return {
            "status": res.status_code,
            "ok": 200 <= res.status_code < 300,
            "json": body,
            "text_sample": res.text[:500],
            "content_type": res.headers.get("content-type", ""),
        }

    def request(self, method, path, **kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            headers.setdefault("X-CSRF-Token", self.refresh_csrf())
        res = self.session.request(method, f"{self.base_url}{path}", headers=headers, timeout=90, **kwargs)
        return self.capture(res)


def add_check(out, name, ok, detail=None, severity="medium"):
    row = {"name": name, "ok": bool(ok), "detail": detail or {}}
    out["checks"].append(row)
    if not ok:
        out["findings"].append({"severity": severity, "title": name, "detail": detail or {}})


def write_fixtures(root):
    root.mkdir(parents=True, exist_ok=True)
    fixtures = {}
    fixtures["txt"] = root / "qa-note.txt"
    fixtures["txt"].write_text("hello preview\nsecond line\n", encoding="utf-8")
    fixtures["md"] = root / "qa-markdown.md"
    fixtures["md"].write_text("# QA Markdown\n\n- item\n", encoding="utf-8")
    fixtures["json"] = root / "qa-data.json"
    fixtures["json"].write_text(json.dumps({"alpha": 1, "nested": {"ok": True}}), encoding="utf-8")
    fixtures["html"] = root / "qa-page.html"
    fixtures["html"].write_text("<!doctype html><title>qa</title><p>inline html</p>", encoding="utf-8")
    fixtures["pdf"] = root / "qa.pdf"
    fixtures["pdf"].write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    fixtures["png"] = root / "qa.png"
    fixtures["png"].write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000a49444154789c6360000002000100ffff03000006000557bfabcc0000000049454e44ae426082"
    ))
    fixtures["zip"] = root / "qa.zip"
    with zipfile.ZipFile(fixtures["zip"], "w") as zf:
        zf.writestr("inside.txt", "zip preview")
    fixtures["mp4"] = root / "qa-video.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=160x90:d=1",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
        "-shortest", "-pix_fmt", "yuv420p", str(fixtures["mp4"]),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    fixtures["torrent"] = root / "qa-localhost-tracker.torrent"
    fixtures["torrent"].write_bytes(
        b"d8:announce25:http://127.0.0.1/announce4:infod6:lengthi1e4:name6:qa.bin"
        b"12:piece lengthi16384e6:pieces20:aaaaaaaaaaaaaaaaaaaaee"
    )
    return fixtures


def upload_storage(client, path, privacy_mode, virtual_path, extra_data=None):
    mime = {
        ".txt": "text/plain", ".md": "text/markdown", ".json": "application/json",
        ".html": "text/html", ".pdf": "application/pdf", ".png": "image/png", ".zip": "application/zip",
    }.get(path.suffix.lower(), "application/octet-stream")
    with path.open("rb") as fh:
        return client.request(
            "POST", "/api/storage/files",
            files={"file": (path.name, fh, mime)},
            data={"privacy_mode": privacy_mode, "virtual_path": virtual_path, "display_name": path.name, **(extra_data or {})},
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--root-password", default=os.environ.get("HACKME_QA_ROOT_PASSWORD", ""), help=argparse.SUPPRESS)
    parser.add_argument("--test-password", default=os.environ.get("HACKME_QA_TEST_PASSWORD", ""), help=argparse.SUPPRESS)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if not str(args.root_password or "").strip():
        parser.error("root password is required through HACKME_QA_ROOT_PASSWORD")
    if not str(args.test_password or "").strip():
        parser.error("test password is required through HACKME_QA_TEST_PASSWORD")
    requests.packages.urllib3.disable_warnings()

    out = {"base_url": args.base_url, "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "checks": [], "findings": []}
    out_path = Path(args.out)
    fixtures = write_fixtures(out_path.parent / "member_probe_artifacts")
    run_id = str(int(time.time()))

    root = Client(args.base_url, "root", args.root_password)
    test = Client(args.base_url, "test", args.test_password)
    root_login = root.login()
    test_login = test.login()
    add_check(out, "root login", root_login["ok"], root_login, "critical")
    add_check(out, "test member login", test_login["ok"], test_login, "critical")

    root_me = root.request("GET", "/api/me")
    root_user_id = ((root_me.get("json") or {}).get("user") or {}).get("id")

    storage_ids = {}
    for key in ["txt", "md", "json", "html", "pdf", "png", "zip"]:
        up = upload_storage(test, fixtures[key], "standard_plain", f"/QA/{run_id}/{key}", {"display_name": f"{run_id}-{fixtures[key].name}"})
        file_id = (((up.get("json") or {}).get("file") or {}).get("file_id"))
        storage_ids[key] = (((up.get("json") or {}).get("storage_file") or {}).get("id"))
        prev = test.request("GET", f"/api/cloud-drive/files/{file_id}/preview") if file_id else {"ok": False}
        add_check(out, f"drive upload and preview {key}", up["ok"] and prev["ok"], {"upload": up, "preview": prev}, "high")

    e2ee_data = {
        "encrypted_metadata": json.dumps({"name": "qa-note.txt"}),
        "encrypted_file_key": "qa-wrapped-file-key",
        "wrapped_by": "qa-public-key",
        "ciphertext_sha256": "0" * 64,
        "encryption_algorithm": "AES-GCM",
        "encryption_version": "qa-v1",
        "nonce": "qa-nonce",
    }
    e2ee_up = upload_storage(test, fixtures["txt"], "e2ee", f"/QA/{run_id}/e2ee", e2ee_data)
    e2ee_file_id = (((e2ee_up.get("json") or {}).get("file") or {}).get("file_id"))
    e2ee_prev = test.request("GET", f"/api/cloud-drive/files/{e2ee_file_id}/preview") if e2ee_file_id else {"ok": False}
    add_check(out, "drive E2EE upload rejects server preview explicitly", e2ee_up["ok"] and e2ee_prev.get("status") == 403, {"upload": e2ee_up, "preview": e2ee_prev}, "high")

    bad_e2ee = upload_storage(test, fixtures["txt"], "e2ee", f"/QA/{run_id}/e2ee-missing-key", {"display_name": f"{run_id}-bad-e2ee.txt"})
    add_check(out, "drive malformed E2EE upload returns client error instead of 500", bad_e2ee["status"] in {400, 422}, bad_e2ee, "high")

    if storage_ids.get("txt"):
        share = test.request("POST", "/api/storage/share-links", json={"storage_file_id": storage_ids["txt"], "can_preview": True})
        token = (((share.get("json") or {}).get("share_link") or {}).get("token") or ((share.get("json") or {}).get("share_link") or {}).get("share_token"))
        dl = test.capture(test.session.get(f"{args.base_url}/api/storage/shared/{token}/download", timeout=20, verify=False)) if token else {"ok": False}
        add_check(out, "drive share-link download works", share["ok"] and dl["ok"], {"share": share, "download": dl}, "high")

    album = test.request("POST", "/api/storage/albums", json={"title": f"QA Album {run_id}", "description": "probe", "visibility": "unlisted", "share_password": "AlbumPass123!"})
    album_obj = ((album.get("json") or {}).get("album") or {})
    album_id = album_obj.get("id")
    album_adds = []
    if album_id:
        for key in ["png", "txt"]:
            if storage_ids.get(key):
                album_adds.append(test.request("POST", f"/api/storage/albums/{album_id}/files", json={"storage_file_id": storage_ids[key], "caption": f"cap {key}"}))
    album_url = (((album_obj.get("share_link") or {}).get("url")) or album_obj.get("share_url") or "")
    album_token = album_url.rstrip("/").split("/")[-1] if album_url else ""
    album_locked = test.capture(test.session.get(f"{args.base_url}/api/storage/shared/albums/{album_token}", timeout=20, verify=False)) if album_token else {"ok": False}
    album_ok = test.capture(test.session.get(f"{args.base_url}/api/storage/shared/albums/{album_token}", headers={"X-Album-Share-Password": "AlbumPass123!"}, timeout=20, verify=False)) if album_token else {"ok": False}
    add_check(out, "album password share rejects missing and opens with password", album["ok"] and album_locked.get("status") in {401, 403, 404} and album_ok["ok"], {"album": album, "adds": album_adds, "locked": album_locked, "open": album_ok}, "high")

    cap = test.request("GET", "/api/cloud-drive/remote-download/capabilities")
    direct = test.request("POST", "/api/cloud-drive/remote-download/tasks", json={"url": f"{args.base_url}/api/version", "download_mode": "direct"})
    magnet = test.request("POST", "/api/cloud-drive/remote-download/tasks", json={"url": "magnet:?xt=urn:btih:" + "a" * 40 + "&tr=http://127.0.0.1/announce", "download_mode": "bt"})
    with fixtures["torrent"].open("rb") as fh:
        torrent = test.request("POST", "/api/cloud-drive/remote-download/torrent-tasks", files={"torrent_file": ("qa.torrent", fh, "application/x-bittorrent")})
    task_id = (((torrent.get("json") or {}).get("task") or {}).get("id"))
    torrent_status = None
    if task_id:
        time.sleep(2)
        torrent_status = test.request("GET", f"/api/cloud-drive/remote-download/tasks/{task_id}")
    torrent_status_ok = not task_id or (torrent_status and torrent_status.get("status") == 200)
    add_check(
        out,
        "remote download blocks direct localhost and keeps BT task observable",
        cap["ok"] and direct.get("status") == 400 and magnet.get("status") == 202 and torrent.get("status") == 202 and torrent_status_ok,
        {"capabilities": cap, "direct": direct, "magnet": magnet, "torrent": torrent, "torrent_status": torrent_status},
        "critical",
    )

    with fixtures["mp4"].open("rb") as fh:
        video = test.request("POST", "/api/videos/upload", files={"video": ("qa-video.mp4", fh, "video/mp4")}, data={"title": "QA video", "visibility": "unlisted", "share_password": "VideoPass123!", "share_max_views": "3"})
    video_obj = ((video.get("json") or {}).get("video") or {})
    stream_status = None
    stream_status_url = video_obj.get("stream_status_url") or ""
    if stream_status_url:
        for _ in range(90):
            stream_status = test.request("GET", stream_status_url)
            asset_status = ((((stream_status.get("json") or {}).get("asset") or {}).get("status")) or "")
            if asset_status == "ready":
                break
            if asset_status in {"failed", "unavailable"}:
                break
            time.sleep(1)
    token = (((video_obj.get("share_link") or {}).get("url")) or video_obj.get("share_url") or "").rstrip("/").split("/")[-1]
    unlock_bad = test.request("POST", f"/api/videos/shared/{token}/unlock", json={"password": "wrong"}) if token else {"ok": False}
    unlock_ok = test.request("POST", f"/api/videos/shared/{token}/unlock", json={"password": "VideoPass123!"}) if token else {"ok": False}
    share_session = ((unlock_ok.get("json") or {}).get("share_session_id"))
    playback = test.capture(test.session.get(f"{args.base_url}/api/videos/shared/{token}/playback", params={"share_session": share_session}, timeout=20, verify=False)) if token and share_session else {"ok": False}
    add_check(out, "video upload password share unlock playback", video["ok"] and unlock_bad.get("status") in {401, 403} and unlock_ok["ok"] and playback["ok"], {"upload": video, "stream_status": stream_status, "wrong": unlock_bad, "unlock": unlock_ok, "playback": playback}, "high")

    with fixtures["mp4"].open("rb") as fh:
        unsupported = test.request("POST", "/api/videos/upload", files={"video": ("qa-video.mp4", fh, "video/mp4")}, data={"privacy_mode": "e2ee", "title": "bad e2ee"})
    add_check(out, "video upload rejects unsupported E2EE mode explicitly", unsupported.get("status") == 400 and "unsupported_privacy_mode" in json.dumps(unsupported.get("json") or {}), unsupported, "medium")

    grid_payload = {"market_symbol": "ETH/USDT", "lower_price_points": 100000, "upper_price_points": 100200, "grid_count": 2, "order_amount_points": 10000, "order_mode": "maker"}
    grid = test.request("POST", "/api/trading/grid/preview", json=grid_payload)
    gp = ((grid.get("json") or {}).get("grid_profit") or {})
    fm = ((grid.get("json") or {}).get("fee_model") or {})
    buy_notional = Decimal(str(grid_payload["order_amount_points"]))
    sell = Decimal(str(gp.get("reference_sell_price_points", "0")))
    qty = Decimal(str(gp.get("reference_quantity", "0")))
    buy_fee_rate = Decimal(str(fm.get("buy_fee_percent", "0"))) / Decimal("100")
    sell_fee_rate = Decimal(str(fm.get("sell_fee_percent", "0"))) / Decimal("100")
    sell_notional = qty * sell
    expected_fee = (buy_notional * buy_fee_rate) + (sell_notional * sell_fee_rate)
    expected_net = (sell_notional - buy_notional) - expected_fee
    quant = Decimal("0.00000001")
    grid_ok = grid["ok"] and Decimal(str(gp.get("estimated_fee_per_grid"))) == expected_fee.quantize(quant) and Decimal(str(gp.get("estimated_net_profit_per_grid"))) == expected_net.quantize(quant)
    add_check(out, "trading grid fee math numeric equality", grid_ok, {"preview": grid, "expected_fee": str(expected_fee.quantize(quant)), "expected_net": str(expected_net.quantize(quant))}, "critical")

    if root_user_id:
        credit = root.request("POST", "/api/admin/points/adjust", json={"user_id": root_user_id, "direction": "credit", "amount": 1234, "reason": "FULL_QA_ROOT_RESERVE_SEED", "idempotency_key": f"fullqa-credit-{run_id}"})
        reserve = root.request("POST", "/api/root/trading/reserve/allocate", json={"source_user_id": root_user_id, "amount_points": 123, "reason": "ROOT_RESERVE_ALLOCATION"})
        verify = root.request("GET", "/api/root/trading/verify")
        add_check(out, "reserve allocation and verification with required reason", credit["ok"] and reserve["ok"] and verify["ok"] and not (((verify.get("json") or {}).get("errors"))), {"credit": credit, "reserve": reserve, "verify": verify}, "critical")

    out["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(out_path)
    if out["findings"]:
        for finding in out["findings"]:
            print(f"{finding['severity']}: {finding['title']}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
