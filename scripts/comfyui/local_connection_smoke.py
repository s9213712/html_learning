#!/usr/bin/env python3
"""Smoke-test root ComfyUI local connection / autostart path."""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.security.common_paths import timestamped_security_report_paths  # noqa: E402


class ProbeError(RuntimeError):
    pass


class WebClient:
    def __init__(self, base_url: str, *, insecure: bool = False):
        self.base_url = str(base_url).rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if self.base_url.startswith("https://"):
            ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [("User-Agent", "hackme_web-comfyui-local-smoke/1.0")]
        self.csrf_token = ""

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def request(self, path: str, *, method: str = "GET", payload=None, allow_http_error: bool = False):
        headers = {}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            if self.csrf_token:
                headers["X-CSRF-Token"] = self.csrf_token
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._url(path), data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = int(resp.status)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = int(exc.code)
            if not allow_http_error:
                raise
        try:
            data = json.loads(raw)
        except Exception as exc:
            raise ProbeError(f"{path} 回應不是 JSON（HTTP {status}）") from exc
        data["_http_status"] = status
        return data

    def fetch_csrf(self) -> str:
        payload = self.request("/api/csrf-token")
        token = str(payload.get("csrf_token") or "").strip()
        if not token:
            raise ProbeError("伺服器沒有回傳 csrf_token")
        self.csrf_token = token
        return token

    def login(self, username: str, password: str):
        self.fetch_csrf()
        payload = self.request(
            "/api/login",
            method="POST",
            payload={"username": username, "password": password},
            allow_http_error=True,
        )
        if payload.get("_http_status") != 200 or payload.get("ok") is not True:
            raise ProbeError(f"登入失敗：{payload.get('msg') or payload.get('message') or 'unknown error'}")
        self.fetch_csrf()
        return payload


def _report_row(name: str, *, ok: bool, detail: str = "", payload=None):
    return {
        "name": name,
        "ok": bool(ok),
        "detail": detail,
        "payload": payload or {},
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def render_markdown(report: dict) -> str:
    lines = [
        "# ComfyUI Local Connection Smoke",
        "",
        f"- base_url: `{report['base_url']}`",
        f"- ok: `{report['ok']}`",
        f"- connection_mode: `{report.get('connection_mode') or '-'}`",
        f"- comfyui_url: `{report.get('comfyui_url') or '-'}`",
        "",
        "## Checks",
        "",
    ]
    for row in report.get("results") or []:
        lines.extend([
            f"### {row['name']}",
            "",
            f"- ok: `{row['ok']}`",
            f"- detail: {row['detail'] or '-'}",
            "",
        ])
    return "\n".join(lines)


def write_reports(report: dict, *, out_json: str | None, out_md: str | None):
    if not out_json and not out_md:
        out_json_path, out_md_path = timestamped_security_report_paths("comfyui_local_connection_smoke")
    else:
        out_json_path = Path(out_json) if out_json else None
        out_md_path = Path(out_md) if out_md else None
    if out_json_path:
        out_json_path.parent.mkdir(parents=True, exist_ok=True)
        out_json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    if out_md_path:
        out_md_path.parent.mkdir(parents=True, exist_ok=True)
        out_md_path.write_text(render_markdown(report), encoding="utf-8")
    return str(out_json_path) if out_json_path else "", str(out_md_path) if out_md_path else ""


def parse_args():
    parser = argparse.ArgumentParser(description="Smoke-test root ComfyUI local connection and autostart path.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", default="root")
    parser.add_argument(
        "--password",
        default=os.environ.get("HACKME_ROOT_PASSWORD", ""),
        help="Root password. Prefer HACKME_ROOT_PASSWORD over a command-line value.",
    )
    parser.add_argument("--comfyui-base-dir", required=True)
    parser.add_argument("--comfyui-local-script", required=True)
    parser.add_argument("--comfyui-api-host", default="127.0.0.1")
    parser.add_argument("--comfyui-api-port", type=int, default=8192)
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--out-json", default="")
    parser.add_argument("--out-md", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.password:
        raise SystemExit("HACKME_ROOT_PASSWORD or --password is required")
    client = WebClient(args.base_url, insecure=args.insecure)
    results = []
    report = {
        "base_url": args.base_url,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        client.login(args.username, args.password)
        results.append(_report_row("root login", ok=True))
        payload = client.request(
            "/api/root/comfyui/test-connection",
            method="POST",
            payload={
                "mode": "local",
                "host": args.comfyui_api_host,
                "port": int(args.comfyui_api_port),
                "base_dir": args.comfyui_base_dir,
                "local_start_script": args.comfyui_local_script,
            },
            allow_http_error=True,
        )
        available = payload.get("available") is True
        local_script = payload.get("local_script") or {}
        ok = (
            payload.get("_http_status") == 200
            and payload.get("ok") is True
            and available
            and str(payload.get("connection_mode") or "") == "local"
            and bool(local_script.get("exists"))
            and bool(local_script.get("syntax_ok"))
        )
        results.append(
            _report_row(
                "root comfyui local connection",
                ok=ok,
                detail=str(payload.get("msg") or (payload.get("autostart") or {}).get("message") or ""),
                payload={
                    "http_status": payload.get("_http_status"),
                    "available": payload.get("available"),
                    "connection_mode": payload.get("connection_mode"),
                    "comfyui_url": payload.get("comfyui_url"),
                    "local_script": local_script,
                    "autostart": payload.get("autostart") or {},
                },
            )
        )
        report.update(
            {
                "ok": all(row["ok"] for row in results),
                "results": results,
                "connection_mode": payload.get("connection_mode"),
                "comfyui_url": payload.get("comfyui_url"),
            }
        )
    except Exception as exc:
        results.append(_report_row("fatal", ok=False, detail=str(exc)))
        report.update({"ok": False, "results": results, "error": str(exc)})
    out_json, out_md = write_reports(report, out_json=args.out_json or None, out_md=args.out_md or None)
    print(
        json.dumps(
            {
                "ok": report["ok"],
                "json_report": out_json,
                "md_report": out_md,
                "results": report["results"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
