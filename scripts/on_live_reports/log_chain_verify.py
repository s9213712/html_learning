#!/usr/bin/env python3
"""On-live-report driver: log_chain_verify.

Calls the root API ``GET /api/root/server-mode/logs/verify`` against a running
hackme_web instance and prints the JSON response. Exit code is 0 if the chain
is intact (`ok=true`), 1 otherwise.

Usage:
    export HACKME_PROBE_ROOT_PASSWORD
    python3 scripts/on_live_reports/log_chain_verify.py \
        --base-url "https://127.0.0.1:$PORT"
"""
import argparse
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request
import ssl


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.testing.probe_credentials import add_root_password_argument  # noqa: E402


def _post(url, data=None, cookies=None, csrf=None, ctx=None):
    req = urllib.request.Request(url, data=data, method="POST")
    if cookies:
        req.add_header("Cookie", cookies)
    if csrf:
        req.add_header("X-CSRF-Token", csrf)
    if data:
        req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req, context=ctx)


def _get(url, cookies=None, ctx=None):
    req = urllib.request.Request(url, method="GET")
    if cookies:
        req.add_header("Cookie", cookies)
    return urllib.request.urlopen(req, context=ctx)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="https://127.0.0.1:5000")
    add_root_password_argument(p)
    p.add_argument("--insecure", action="store_true", default=True, help="skip TLS verify")
    args = p.parse_args(argv)

    ctx = ssl._create_unverified_context() if args.insecure else None
    base = args.base_url.rstrip("/")

    csrf_resp = _get(f"{base}/api/csrf-token", ctx=ctx)
    cookies = csrf_resp.headers.get("Set-Cookie", "").split(";", 1)[0]
    csrf = json.loads(csrf_resp.read())["csrf_token"]

    login_body = json.dumps({"username": "root", "password": args.root_password}).encode()
    login_resp = _post(f"{base}/api/login", data=login_body, cookies=cookies, csrf=csrf, ctx=ctx)
    set_cookies = login_resp.headers.get_all("Set-Cookie") or []
    cookies = "; ".join(c.split(";", 1)[0] for c in set_cookies) or cookies

    verify_resp = _get(f"{base}/api/root/server-mode/logs/verify", cookies=cookies, ctx=ctx)
    payload = json.loads(verify_resp.read())
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
