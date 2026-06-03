#!/usr/bin/env python3
"""Deep Playwright smoke/debug run for hackme_web.

The script intentionally starts hackme_web with an isolated /tmp runtime so a
QA pass does not write runtime databases, logs, uploads, or chess models into
the repository checkout.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import chess
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[2]
ROOT_PASSWORD = "RootDeep123!"
MANAGER_PASSWORD = "ManagerDeep123!"
TEST_PASSWORD = "TestDeep123!"
JOURNEY_PASSWORD = "JourneyDeep123!"


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class Recorder:
    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def add(self, name: str, ok: bool, detail: str = "", **data: Any) -> None:
        self.results.append(CheckResult(name=name, ok=ok, detail=detail, data=data))
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}: {detail}", flush=True)

    def guard(self, name: str, fn) -> Any:
        before = len(self.results)
        try:
            value = fn()
            if len(self.results) == before:
                self.add(name, True)
            return value
        except Exception as exc:
            self.add(name, False, f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
            return None


@dataclass
class OptionalComfyUIConfig:
    enabled: bool = False
    remote_api_url: str = ""
    local_base_dir: str = ""
    local_start_script: str = ""
    local_api_host: str = ""
    local_api_port: int | None = None
    civitai_api_key: str = ""
    civitai_query: str = "sdxl"
    civitai_model_type: str = "checkpoint"
    civitai_source: str = "all"

    def has_live_civitai(self) -> bool:
        return bool(self.civitai_api_key.strip())

    def has_live_comfyui(self) -> bool:
        return bool(self.remote_api_url.strip() or self.local_base_dir.strip() or self.local_start_script.strip())

    def safe_summary(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "remote_api_url": self.remote_api_url,
            "local_base_dir": self.local_base_dir,
            "local_start_script": self.local_start_script,
            "local_api_host": self.local_api_host,
            "local_api_port": self.local_api_port,
            "civitai_api_key_provided": bool(self.civitai_api_key.strip()),
            "civitai_query": self.civitai_query,
            "civitai_model_type": self.civitai_model_type,
            "civitai_source": self.civitai_source,
        }


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def prompt_text(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default and not secret else ""
    prompt = f"{label}{suffix}: "
    if secret:
        value = getpass.getpass(prompt)
    else:
        value = input(prompt)
    return value.strip() or default


def prompt_yes_no(label: str, default: bool = False) -> bool:
    marker = "Y/n" if default else "y/N"
    value = input(f"{label} ({marker}): ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "true", "1", "是", "對", "好"}


def collect_optional_comfyui_config(args: argparse.Namespace) -> OptionalComfyUIConfig:
    cfg = OptionalComfyUIConfig(
        enabled=bool(args.comfyui_api_url or args.comfyui_base_dir or args.comfyui_start_script or args.civitai_api_key),
        remote_api_url=args.comfyui_api_url.strip(),
        local_base_dir=args.comfyui_base_dir.strip(),
        local_start_script=args.comfyui_start_script.strip(),
        local_api_host=args.comfyui_api_host.strip(),
        local_api_port=args.comfyui_api_port,
        civitai_api_key=args.civitai_api_key.strip(),
        civitai_query=args.civitai_live_query.strip() or "sdxl",
        civitai_model_type=args.civitai_live_model_type.strip() or "checkpoint",
        civitai_source=args.civitai_live_source.strip() or "all",
    )
    if not args.interactive_comfyui:
        return cfg
    if not sys.stdin.isatty():
        print("[WARN] --interactive-comfyui was requested, but stdin is not a TTY; using CLI/env values only.", flush=True)
        return cfg
    if not prompt_yes_no("是否輸入 ComfyUI / Civitai 實測設定？不輸入會維持離線 guard 檢查", default=False):
        return cfg
    cfg.enabled = True
    cfg.remote_api_url = prompt_text("ComfyUI remote API URL，格式需為 http(s)://host:port；若要測本機啟動可留空", cfg.remote_api_url)
    if not cfg.remote_api_url:
        cfg.local_base_dir = prompt_text("ComfyUI 本機資料夾位置；可留空跳過本機啟動測試", cfg.local_base_dir)
        if cfg.local_base_dir:
            cfg.local_start_script = prompt_text("ComfyUI 啟動腳本名稱或相對路徑；可留空只測現有連線", cfg.local_start_script)
            cfg.local_api_host = prompt_text("ComfyUI API host", cfg.local_api_host or "localhost")
            raw_port = prompt_text("ComfyUI API port", str(cfg.local_api_port or 8192))
            try:
                cfg.local_api_port = int(raw_port)
            except ValueError:
                cfg.local_api_port = None
    if prompt_yes_no("是否輸入 Civitai API Key 做真實搜尋縮圖/網址檢查？", default=bool(cfg.civitai_api_key)):
        cfg.civitai_api_key = prompt_text("Civitai API Key（輸入時不顯示）", cfg.civitai_api_key, secret=True)
        cfg.civitai_query = prompt_text("Civitai 搜尋關鍵字", cfg.civitai_query)
        cfg.civitai_model_type = prompt_text("Civitai 模型種類", cfg.civitai_model_type)
        cfg.civitai_source = prompt_text("Civitai 搜尋來源 all/com/red", cfg.civitai_source)
    return cfg


def env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def mkdirs(runtime_root: Path) -> None:
    for rel in (
        "database",
        "logs",
        "chats",
        "anchors",
        "storage",
        "reports/qa",
        "games/models",
        "games/replays",
        "certs",
        "secrets",
    ):
        (runtime_root / rel).mkdir(parents=True, exist_ok=True)


def build_env(runtime_root: Path, port: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "HACKME_RUNTIME_DIR": str(runtime_root),
            "HTML_LEARNING_HOST": "127.0.0.1",
            "HTML_LEARNING_PORT": str(port),
            "HTML_LEARNING_DB_DIR": str(runtime_root / "database"),
            "HTML_LEARNING_LOG_DIR": str(runtime_root / "logs"),
            "HTML_LEARNING_CHAT_DIR": str(runtime_root / "chats"),
            "HTML_LEARNING_ANCHOR_DIR": str(runtime_root / "anchors"),
            "HTML_LEARNING_STORAGE_DIR": str(runtime_root / "storage"),
            "HTML_LEARNING_REPORTS_DIR": str(runtime_root / "reports"),
            "HTML_LEARNING_RUNTIME_SECRETS_DIR": str(runtime_root / "secrets"),
            "HTML_LEARNING_CERT_FILE": str(runtime_root / "certs" / "cert.pem"),
            "HTML_LEARNING_KEY_FILE": str(runtime_root / "certs" / "key.pem"),
            "HTML_LEARNING_ROOT_PASSWORD": ROOT_PASSWORD,
            "HTML_LEARNING_MANAGER_PASSWORD": MANAGER_PASSWORD,
            "HTML_LEARNING_TEST_PASSWORD": TEST_PASSWORD,
            "HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_POLICY": "1",
            "HTML_LEARNING_CHESS_ENGINE_DB_PATH": str(runtime_root / "games" / "models" / "chess_experiment.db"),
            "HTML_LEARNING_CHESS_ENGINE_NN_MODEL_PATH": str(runtime_root / "games" / "models" / "chess_nn.json"),
            "HTML_LEARNING_CHESS_ENGINE_DL_MODEL_PATH": str(runtime_root / "games" / "models" / "chess_dl.pt"),
            "HTML_LEARNING_CHESS_ENGINE_PV_MODEL_PATH": str(runtime_root / "games" / "models" / "chess_pv.json"),
            "HTML_LEARNING_CHESS_REPLAY_BUFFER_PATH": str(runtime_root / "games" / "replays" / "chess_replay_buffer.jsonl"),
            "HTML_LEARNING_CHESS_REPLAY_QUARANTINE_PATH": str(runtime_root / "games" / "replays" / "chess_replay_quarantine.jsonl"),
            "HTML_LEARNING_CHESS_REPLAY_REJECTED_PATH": str(runtime_root / "games" / "replays" / "chess_replay_rejected.jsonl"),
        }
    )
    return env


def urlopen_json(url: str, timeout: float = 2.0) -> dict[str, Any] | None:
    ctx = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return json.loads(body)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ssl.SSLError):
        return None


def wait_for_server(port: int, timeout_seconds: int = 45) -> str:
    deadline = time.time() + timeout_seconds
    urls = [f"https://127.0.0.1:{port}", f"http://127.0.0.1:{port}"]
    while time.time() < deadline:
        for base_url in urls:
            payload = urlopen_json(base_url + "/api/version")
            if payload and payload.get("ok"):
                return base_url
        time.sleep(0.5)
    raise RuntimeError(f"server did not become ready on port {port}")


def start_server(runtime_root: Path, port: int) -> subprocess.Popen[str]:
    log_file = runtime_root / "logs" / "playwright_server.out"
    env = build_env(runtime_root, port)
    return subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log_file.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
    )


def cookie_value(page, name: str) -> str:
    return page.evaluate(
        """name => {
            const part = document.cookie.split('; ').find(item => item.startsWith(name + '='));
            return part ? decodeURIComponent(part.split('=').slice(1).join('=')) : '';
        }""",
        name,
    )


def fetch_json(page, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    csrf = cookie_value(page, "csrf_token")
    return page.evaluate(
        """async ({method, path, payload, csrf}) => {
            const cookieValue = name => {
                const part = document.cookie.split('; ').find(item => item.startsWith(name + '='));
                return part ? decodeURIComponent(part.split('=').slice(1).join('=')) : '';
            };
            const send = async token => {
                const opts = {
                    method,
                    credentials: 'same-origin',
                    headers: {'Accept': 'application/json', 'X-CSRF-Token': token || ''}
                };
                if (payload !== null) {
                    opts.headers['Content-Type'] = 'application/json';
                    opts.body = JSON.stringify(payload);
                }
                const response = await fetch(path, opts);
                const text = await response.text();
                let body = null;
                try { body = text ? JSON.parse(text) : null; } catch (err) { body = {raw: text.slice(0, 500)}; }
                return {status: response.status, ok: response.ok, body};
            };
            let result = await send(csrf || cookieValue('csrf_token'));
            if (method !== 'GET' && result.status === 403 && result.body && result.body.error === 'csrf_invalid') {
                await fetch('/api/csrf-token', {credentials: 'same-origin'});
                result = await send(cookieValue('csrf_token'));
                result.csrf_retry = true;
            }
            return result;
        }""",
        {"method": method, "path": path, "payload": payload, "csrf": csrf},
    )


def fetch_text(page, path: str) -> dict[str, Any]:
    return page.evaluate(
        """async path => {
            const response = await fetch(path, {credentials: 'same-origin'});
            const text = await response.text();
            return {
                status: response.status,
                ok: response.ok,
                text: text.slice(0, 4000),
                contentType: response.headers.get('content-type') || ''
            };
        }""",
        path,
    )


def fetch_multipart(page, path: str, fields: dict[str, Any], files: list[dict[str, Any]]) -> dict[str, Any]:
    csrf = cookie_value(page, "csrf_token")
    return page.evaluate(
        """async ({path, fields, files, csrf}) => {
            const cookieValue = name => {
                const part = document.cookie.split('; ').find(item => item.startsWith(name + '='));
                return part ? decodeURIComponent(part.split('=').slice(1).join('=')) : '';
            };
            const buildForm = () => {
                const form = new FormData();
                for (const [key, value] of Object.entries(fields || {})) {
                    form.append(key, String(value));
                }
                for (const file of files || []) {
                    const bytes = new Uint8Array(file.bytes || []);
                    const blob = new Blob([bytes], {type: file.type || 'application/octet-stream'});
                    form.append(file.field || 'file', blob, file.name || 'upload.bin');
                }
                return form;
            };
            const send = async token => {
                const response = await fetch(path, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: {'Accept': 'application/json', 'X-CSRF-Token': token || ''},
                    body: buildForm()
                });
                const text = await response.text();
                let body = null;
                try { body = text ? JSON.parse(text) : null; } catch (err) { body = {raw: text.slice(0, 500)}; }
                return {status: response.status, ok: response.ok, body};
            };
            let result = await send(csrf || cookieValue('csrf_token'));
            if (result.status === 403 && result.body && result.body.error === 'csrf_invalid') {
                await fetch('/api/csrf-token', {credentials: 'same-origin'});
                result = await send(cookieValue('csrf_token'));
                result.csrf_retry = true;
            }
            return result;
        }""",
        {"path": path, "fields": fields, "files": files, "csrf": csrf},
    )


def text_file(name: str, content: str, *, field: str = "file", mime: str = "text/plain") -> dict[str, Any]:
    return {
        "field": field,
        "name": name,
        "type": mime,
        "bytes": list(content.encode("utf-8")),
    }


def bytes_file(name: str, content: bytes, *, field: str = "file", mime: str = "application/octet-stream") -> dict[str, Any]:
    return {"field": field, "name": name, "type": mime, "bytes": list(content)}


def generate_tiny_mp4() -> bytes:
    fallback = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isomqa"
    output = Path(tempfile.gettempdir()) / f"hackme_web_qa_video_{os.getpid()}_{int(time.time() * 1000)}.mp4"
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=mono:sample_rate=8000",
                "-shortest",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-movflags",
                "+faststart",
                str(output),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        if result.returncode == 0 and output.exists() and output.stat().st_size > 0:
            return output.read_bytes()
    except Exception:
        pass
    finally:
        try:
            output.unlink()
        except FileNotFoundError:
            pass
    return fallback


def attach_browser_error_handlers(page, record_error) -> None:
    def on_console(msg) -> None:
        if msg.type not in {"error"}:
            return
        text = str(msg.text or "")
        if "Failed to load resource: the server responded with a status of 400" in text:
            return
        if "Failed to load resource: the server responded with a status of 401" in text:
            return
        if "Failed to load resource: the server responded with a status of 403" in text:
            return
        if "Failed to load resource: net::ERR_NETWORK_CHANGED" in text:
            return
        record_error("console", f"{msg.type}: {text}")

    page.on("console", on_console)
    page.on("pageerror", lambda exc: record_error("pageerror", str(exc)))
    page.on(
        "response",
        lambda response: record_error("http", f"{response.status} {response.url}") if response.status >= 500 else None,
    )


def check_ui_quality(rec: Recorder, page, label: str, *, mobile: bool = False) -> None:
    result = page.evaluate(
        """({mobile}) => {
            const isVisible = (el) => {
                const style = getComputedStyle(el);
                const box = el.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden' && box.width > 0 && box.height > 0;
            };
            const problems = [];
            const warnings = [];
            const rootOverflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
            if (rootOverflow > 6) problems.push(`page horizontal overflow ${rootOverflow}px`);
            document.querySelectorAll('button, a.btn, input, select, textarea').forEach((el) => {
                if (!isVisible(el)) return;
                const box = el.getBoundingClientRect();
                const name = (el.id || el.getAttribute('aria-label') || el.textContent || el.name || el.tagName).trim().slice(0, 80);
                if (mobile && (box.width < 44 || box.height < 32) && el.tagName !== 'INPUT') {
                    warnings.push(`small mobile target ${name} ${Math.round(box.width)}x${Math.round(box.height)}`);
                }
                if (el.tagName !== 'SELECT' && el.scrollWidth - el.clientWidth > 10 && box.width > 20) {
                    warnings.push(`text/content clipped ${name}`);
                }
            });
            document.querySelectorAll('.module-section.active .msg, .module-section.active [id$="-msg"], .module-section.active [id$="-status"]').forEach((el) => {
                if (!isVisible(el)) return;
                const text = (el.textContent || '').trim();
                if (/undefined|null|NaN|\\[object Object\\]/i.test(text)) {
                    problems.push(`bad placeholder text ${el.id || el.className}: ${text.slice(0, 120)}`);
                }
            });
            return {problems: problems.slice(0, 80), warnings: warnings.slice(0, 80)};
        }""",
        {"mobile": mobile},
    )
    problems = result.get("problems") or []
    warnings = result.get("warnings") or []
    if problems:
        detail = "; ".join(problems[:8])
    elif warnings:
        detail = "warnings: " + "; ".join(warnings[:8])
    else:
        detail = "ok"
    rec.add(f"ui_quality_{label}", not problems, detail, issues=problems, warnings=warnings)


def wait_for_auth_app(page, *, timeout: int = 30000) -> None:
    page.wait_for_function(
        "() => document.body.classList.contains('app-authenticated') && typeof switchModuleTab === 'function'",
        timeout=timeout,
    )


def switch_module(page, module: str) -> None:
    wait_for_auth_app(page)
    page.evaluate(
        """module => {
            if (typeof switchModuleTab !== 'function') throw new Error('switchModuleTab missing');
            switchModuleTab(module);
        }""",
        module,
    )
    page.wait_for_selector(f"#module-{module}.active", state="visible", timeout=8000)
    page.wait_for_timeout(300)


def switch_server_tab(page, tab: str) -> None:
    switch_module(page, "server")
    page.evaluate(
        """tab => {
            if (typeof switchServerTab !== 'function') throw new Error('switchServerTab missing');
            switchServerTab(tab);
        }""",
        tab,
    )
    page.wait_for_timeout(800)


def switch_admin_tab(page, tab: str) -> None:
    switch_module(page, "accounts")
    page.evaluate(
        """tab => {
            if (typeof switchAdminTab !== 'function') throw new Error('switchAdminTab missing');
            switchAdminTab(tab);
        }""",
        tab,
    )
    page.wait_for_timeout(800)


def login(page, base_url: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    if not cookie_value(page, "csrf_token"):
        page.evaluate("() => fetch('/api/csrf-token', {credentials: 'same-origin'})")
    login_result = fetch_json(page, "POST", "/api/login", {"username": "root", "password": ROOT_PASSWORD})
    if login_result["status"] != 200 or not login_result["body"].get("ok"):
        raise RuntimeError(f"login api failed: {login_result}")
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(300)
    me = fetch_json(page, "GET", "/api/me")
    if me["status"] != 200 or not me["body"].get("ok"):
        raise RuntimeError(f"login failed: {me}")


def enable_required_features(page, base_url: str) -> None:
    feature_keys = [
        "feature_accounts_enabled",
        "feature_chat_enabled",
        "feature_community_enabled",
        "feature_appeals_enabled",
        "feature_audit_log_enabled",
        "feature_violation_center_enabled",
        "feature_reports_enabled",
        "feature_system_health_enabled",
        "feature_identity_governance_enabled",
        "feature_account_security_enabled",
        "feature_member_governance_enabled",
        "feature_server_modes_enabled",
        "feature_snapshot_restore_enabled",
        "feature_health_center_enabled",
        "feature_forum_core_enabled",
        "feature_ui_rebuild_enabled",
        "feature_reports_notifications_enabled",
        "feature_attachments_enabled",
        "feature_privacy_uploads_enabled",
        "feature_storage_albums_enabled",
        "feature_personalization_enabled",
        "feature_social_search_enabled",
        "feature_advanced_security_enabled",
        "feature_comfyui_enabled",
        "feature_economy_enabled",
        "feature_trading_enabled",
        "feature_games_enabled",
        "feature_videos_enabled",
    ]
    updates = {key: True for key in feature_keys}
    result = fetch_json(page, "PUT", "/api/admin/features", updates)
    if result["status"] != 200 or not result["body"].get("ok"):
        raise RuntimeError(f"feature enable failed: {result}")
    page.goto(base_url + "/", wait_until="domcontentloaded")


def apply_optional_comfyui_settings(rec: Recorder, page, cfg: OptionalComfyUIConfig) -> None:
    if not cfg.enabled:
        rec.add("optional_comfyui_settings", True, "not configured; offline checks only", configured=False)
        return
    payload: dict[str, Any] = {}
    if cfg.remote_api_url:
        payload["comfyui_connection_mode"] = "remote"
        payload["comfyui_remote_api_url"] = cfg.remote_api_url
    elif cfg.local_base_dir or cfg.local_start_script:
        payload["comfyui_connection_mode"] = "local"
        if cfg.local_base_dir:
            payload["comfyui_base_dir"] = cfg.local_base_dir
        if cfg.local_start_script:
            payload["comfyui_local_start_script"] = cfg.local_start_script
        if cfg.local_api_host:
            payload["comfyui_api_host"] = cfg.local_api_host
        if cfg.local_api_port:
            payload["comfyui_api_port"] = cfg.local_api_port
    if cfg.civitai_api_key:
        payload["comfyui_civitai_api_key"] = cfg.civitai_api_key
    if not payload:
        rec.add("optional_comfyui_settings", True, "enabled but no settings provided; offline checks only", configured=False)
        return
    result = fetch_json(page, "PUT", "/api/admin/settings", payload)
    body = result.get("body") or {}
    ok = result["status"] == 200 and body.get("ok")
    safe_payload_keys = sorted(payload)
    if not ok:
        raise RuntimeError(f"optional ComfyUI settings rejected: status={result['status']}, msg={body.get('msg')}")
    rec.add(
        "optional_comfyui_settings",
        True,
        f"saved keys: {', '.join(safe_payload_keys)}",
        configured=True,
        keys=safe_payload_keys,
    )


def check_live_comfyui_connection(rec: Recorder, page, cfg: OptionalComfyUIConfig) -> None:
    if not cfg.has_live_comfyui():
        rec.add("comfyui_live_connection_optional", True, "not configured")
        return
    payload: dict[str, Any] = {}
    if cfg.remote_api_url:
        payload["connection_mode"] = "remote"
        payload["comfyui_connection_mode"] = "remote"
        payload["comfyui_remote_api_url"] = cfg.remote_api_url
    else:
        payload["connection_mode"] = "local"
        payload["comfyui_connection_mode"] = "local"
        if cfg.local_base_dir:
            payload["comfyui_base_dir"] = cfg.local_base_dir
        if cfg.local_start_script:
            payload["comfyui_local_start_script"] = cfg.local_start_script
        if cfg.local_api_host:
            payload["comfyui_api_host"] = cfg.local_api_host
        if cfg.local_api_port:
            payload["comfyui_api_port"] = cfg.local_api_port
    result = fetch_json(page, "POST", "/api/root/comfyui/test-connection", payload)
    body = result.get("body") or {}
    ok = result["status"] == 200 and body.get("ok") and body.get("available")
    detail = body.get("comfyui_url") or body.get("msg") or f"status={result['status']}"
    rec.add(
        "comfyui_live_connection",
        ok,
        str(detail)[:240],
        available=bool(body.get("available")),
        starting=bool(body.get("starting")),
        connection_mode=body.get("connection_mode"),
        comfyui_url=body.get("comfyui_url"),
        msg=body.get("msg"),
    )


def check_api_surface(rec: Recorder, page) -> None:
    endpoints = [
        "/api/site-config",
        "/api/version",
        "/api/me",
        "/api/chat/rooms",
        "/api/community/announcements",
        "/api/community/categories",
        "/api/community/boards",
        "/api/storage/files",
        "/api/storage/folders",
        "/api/files/quota",
        "/api/videos",
        "/api/games/catalog",
        "/api/games/users",
        "/api/games/chess/matches",
        "/api/games/chess/leaderboard",
        "/api/points/wallet",
        "/api/points/ledger",
        "/api/points/catalog",
        "/api/points/rules",
        "/api/trading/markets",
        "/api/trading/dashboard",
        "/api/trading/workflow-templates",
        "/api/trading/reference-prices",
        "/api/comfyui/status",
        "/api/comfyui/models",
        "/api/comfyui/history",
        "/api/comfyui/workflow-layouts",
        "/api/admin/health",
        "/api/admin/health/readiness",
        "/api/admin/health/anomaly",
        "/api/admin/health/audit-chain",
        "/api/admin/health/db-integrity",
        "/api/admin/audit",
    ]
    statuses: dict[str, int] = {}
    failures: list[str] = []
    for endpoint in endpoints:
        result = fetch_json(page, "GET", endpoint)
        statuses[endpoint] = int(result["status"])
        if result["status"] >= 400:
            failures.append(f"{endpoint} -> {result['status']}")
    rec.add("authenticated_api_surface", not failures, ", ".join(failures) or f"{len(endpoints)} endpoints <400", statuses=statuses)


def check_auth_registration_journey(rec: Recorder, browser, base_url: str, root_page) -> dict[str, Any]:
    stamp = utc_stamp().lower()
    username = f"qa_{stamp[-8:]}"
    password = JOURNEY_PASSWORD
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})
    page = ctx.new_page()
    try:
        page.goto(base_url + "/", wait_until="domcontentloaded")
        page.click("#tab-register")
        for selector in ("#reg-user", "#reg-pw", "#reg-pw-confirm"):
            page.click(selector)
        page.fill("#reg-user", username)
        page.fill("#reg-pw", password)
        page.fill("#reg-pw-confirm", password)
        page.fill("#reg-nickname", "QA 行動註冊")
        page.fill("#reg-email", f"{username}@example.test")
        page.click("#reg-btn")
        page.locator("#reg-msg").wait_for(state="visible", timeout=8000)
        page.wait_for_function(
            "() => document.querySelector('#reg-msg')?.innerText.trim().length > 0",
            timeout=8000,
        )
        reg_msg = page.locator("#reg-msg").inner_text(timeout=3000)
        pending_login = fetch_json(page, "POST", "/api/login", {"username": username, "password": password})
        users = fetch_json(root_page, "GET", "/api/admin/users?include_deleted=1")
        user_rows = users.get("body", {}).get("users") or []
        target = next((item for item in user_rows if item.get("username") == username), None)
        if not target:
            raise RuntimeError("registered user not found in admin users list")
        review = fetch_json(root_page, "POST", f"/api/admin/users/{int(target['id'])}/review-registration", {"action": "approve"})
        if review["status"] != 200 or not review.get("body", {}).get("ok"):
            raise RuntimeError(f"registration approval failed: {review}")
        approved_login = fetch_json(page, "POST", "/api/login", {"username": username, "password": password})
        ok = (
            "送出" in reg_msg
            and pending_login["status"] in {401, 403, 423}
            and approved_login["status"] == 200
            and approved_login.get("body", {}).get("ok")
        )
        check_ui_quality(rec, page, "auth_mobile_register_login", mobile=True)
        rec.add(
            "auth_registration_login_flow",
            ok,
            f"user={username}, pending_status={pending_login['status']}, approved_status={approved_login['status']}",
            username=username,
            reg_msg=reg_msg,
            pending_login=pending_login.get("body"),
            approved_login=approved_login.get("body"),
        )
        return {"username": username, "password": password, "user_id": int(target["id"])}
    finally:
        try:
            login(root_page, base_url)
        except Exception:
            pass
        ctx.close()


def ensure_journey_user(page, username: str) -> dict[str, Any]:
    users = fetch_json(page, "GET", "/api/admin/users?include_deleted=1")
    for item in users.get("body", {}).get("users") or []:
        if item.get("username") == username:
            return {"id": int(item["id"]), "username": username}
    created = fetch_json(
        page,
        "POST",
        "/api/admin/users",
        {
            "username": username,
            "password": JOURNEY_PASSWORD,
            "password_confirm": JOURNEY_PASSWORD,
            "nickname": "QA Journey",
            "role": "user",
            "status": "active",
            "member_level": "normal",
        },
    )
    if created["status"] not in {200, 409}:
        raise RuntimeError(f"create journey user failed: {created}")
    users = fetch_json(page, "GET", "/api/admin/users?include_deleted=1")
    for item in users.get("body", {}).get("users") or []:
        if item.get("username") == username:
            return {"id": int(item["id"]), "username": username}
    raise RuntimeError("journey user not found after create")


def check_admin_member_management(rec: Recorder, page) -> dict[str, Any]:
    user = ensure_journey_user(page, "qa_journey_user")
    detail = fetch_json(page, "GET", f"/api/admin/users/{user['id']}")
    block = fetch_json(page, "POST", f"/api/admin/users/{user['id']}/block", {"blocked": True, "reason": "playwright deep check"})
    unblock = fetch_json(page, "POST", f"/api/admin/users/{user['id']}/block", {"action": "unblock", "reason": "playwright deep check restore"})
    settings = fetch_json(page, "GET", "/api/admin/settings")
    switch_admin_tab(page, "users")
    users_visible = page.locator("#user-list, #users-list, #admin-user-list").count() > 0 or page.locator("text=帳號").count() > 0
    switch_server_tab(page, "settings")
    check_ui_quality(rec, page, "admin_settings_accounts_desktop")
    ok = (
        detail["status"] == 200
        and block["status"] == 200
        and unblock["status"] == 200
        and settings["status"] == 200
        and users_visible
    )
    rec.add(
        "admin_member_management_flow",
        ok,
        f"user={user['username']}, block={block['status']}, unblock={unblock['status']}, settings={settings['status']}",
        user=user,
        block=block.get("body"),
        unblock=unblock.get("body"),
    )
    return user


def check_forum_journey(rec: Recorder, page) -> dict[str, Any]:
    boards = fetch_json(page, "GET", "/api/community/boards")
    board_rows = boards.get("body", {}).get("boards") or []
    board = next((item for item in board_rows if item.get("status") == "approved" and item.get("visibility") == "public"), None)
    if not board:
        raise RuntimeError("no approved public forum board")
    title = f"Playwright 深度巡檢主題 {utc_stamp()}"
    thread = fetch_json(page, "POST", f"/api/community/boards/{int(board['id'])}/threads", {"title": title, "content": "這是 Playwright 全站巡檢建立的討論區主題。"})
    if thread["status"] != 200 or not thread.get("body", {}).get("ok"):
        raise RuntimeError(f"thread create failed: {thread}")
    thread_list = fetch_json(page, "GET", f"/api/community/boards/{int(board['id'])}/threads?q={title}")
    created_thread = next((item for item in thread_list.get("body", {}).get("threads") or [] if item.get("title") == title), None)
    if not created_thread:
        raise RuntimeError("created thread not found")
    reply = fetch_json(page, "POST", f"/api/community/threads/{int(created_thread['id'])}/posts", {"content": "Playwright 回覆測試：確認留言有回饋。"})
    reaction = fetch_json(page, "POST", f"/api/community/threads/{int(created_thread['id'])}/reaction", {"value": 1})
    switch_module(page, "community")
    page.fill("#community-board-search", str(board.get("title") or ""))
    page.wait_for_timeout(500)
    check_ui_quality(rec, page, "forum_desktop")
    ok = reply["status"] == 200 and reaction["status"] == 200
    rec.add(
        "forum_thread_reply_reaction_flow",
        ok,
        f"board={board.get('title')}, thread_id={created_thread['id']}, reply={reply['status']}, reaction={reaction['status']}",
        board_id=board["id"],
        thread_id=created_thread["id"],
    )
    return {"board_id": int(board["id"]), "thread_id": int(created_thread["id"]), "title": title}


def check_drive_e2ee_journey(rec: Recorder, page) -> dict[str, Any]:
    standard = fetch_multipart(
        page,
        "/api/storage/files",
        {"privacy_mode": "standard_plain", "virtual_path": "/QA/plain-note.txt", "display_name": "plain-note.txt"},
        [text_file("plain-note.txt", "plain cloud drive qa file")],
    )
    e2ee = fetch_multipart(
        page,
        "/api/storage/files",
        {
            "privacy_mode": "e2ee",
            "virtual_path": "/QA/e2ee-note.txt",
            "display_name": "e2ee-note.txt",
            "encrypted_metadata": json.dumps({"name": "e2ee-note.txt", "qa": True}),
            "encrypted_file_key": "qa-wrapped-key",
            "wrapped_by": "playwright",
            "ciphertext_sha256": "0" * 64,
            "encryption_algorithm": "AES-GCM",
            "encryption_version": "client-side-v1",
            "nonce": "qa-nonce",
            "client_scan_report": json.dumps({"claimed_clean": True, "scanner": "playwright"}),
        },
        [text_file("e2ee-note.txt", "ciphertext placeholder for e2ee qa")],
    )
    files = fetch_json(page, "GET", "/api/storage/files")
    switch_module(page, "drive")
    page.wait_for_selector("#module-drive.active #storage-refresh-btn", timeout=8000)
    page.click("#storage-refresh-btn")
    page.wait_for_timeout(900)
    check_ui_quality(rec, page, "drive_e2ee_desktop")
    ok = standard["status"] == 200 and e2ee["status"] == 200 and files["status"] == 200
    rec.add(
        "drive_upload_e2ee_flow",
        ok,
        f"standard={standard['status']}, e2ee={e2ee['status']}, files={files['status']}",
        standard=standard.get("body"),
        e2ee=e2ee.get("body"),
    )
    return {
        "standard_file_id": (standard.get("body", {}).get("file") or {}).get("file_id"),
        "e2ee_file_id": (e2ee.get("body", {}).get("file") or {}).get("file_id"),
    }


def check_video_share_journey(rec: Recorder, page) -> dict[str, Any]:
    video_bytes = generate_tiny_mp4()
    video_path = Path(tempfile.gettempdir()) / f"hackme_web_ui_video_{os.getpid()}_{int(time.time() * 1000)}.mp4"
    video_path.write_bytes(video_bytes)
    try:
        switch_module(page, "videos")
        if page.locator("#video-publish-panel[hidden]").count():
            page.click("#video-publish-open-btn")
            page.wait_for_selector("#video-publish-panel:not([hidden])", timeout=3000)
        page.set_input_files("#video-upload-file", str(video_path))
        page.fill("#video-publish-title", "Playwright QA 影音")
        page.fill("#video-publish-description", "全站巡檢透過前端直接上傳的最小測試影音。")
        page.select_option("#video-publish-visibility", "unlisted")
        page.fill("#video-share-password", "ShareDeep123!")
        page.fill("#video-share-max-views", "3")
        page.click("#video-publish-btn")
        page.wait_for_selector("#video-upload-progress:not([hidden])", timeout=5000)
        progress_seen = page.locator("#video-upload-progress").is_visible(timeout=1000)
        page.wait_for_function(
            """() => {
                const msg = document.querySelector('#video-msg')?.textContent || '';
                const status = document.querySelector('#video-upload-progress-status')?.textContent || '';
                const percent = document.querySelector('#video-upload-progress-percent')?.textContent || '';
                return /影音已發布/.test(msg) || /處理完成/.test(status) || percent.trim() === '100%';
            }""",
            timeout=45000,
        )
        video_msg = page.locator("#video-msg").inner_text(timeout=1000)
        progress_status = page.locator("#video-upload-progress-status").inner_text(timeout=1000)
        progress_percent = page.locator("#video-upload-progress-percent").inner_text(timeout=1000)
        upload_success = (
            "影音已發布" in video_msg
            or "處理完成" in progress_status
            or progress_percent.strip() == "100%"
        )
        videos = fetch_json(page, "GET", "/api/videos")
        page.click("#video-refresh-btn")
        page.wait_for_timeout(900)
        check_ui_quality(rec, page, "videos_desktop")
        videos_body = videos.get("body") or {}
        video_items = videos_body.get("videos") or videos_body.get("items") or videos_body.get("data") or []
        latest = video_items[0] if video_items else {}
        video_id_attr = page.locator("[data-video-like]").first.get_attribute("data-video-like", timeout=2000)
        video_id = int(video_id_attr or latest.get("id") or 0)
        playback = fetch_json(page, "GET", f"/api/videos/{video_id}/playback") if video_id else {"status": 0, "body": {}}
        master = fetch_text(page, (playback.get("body") or {}).get("master_url") or "") if (playback.get("body") or {}).get("master_url") else {"status": 0, "text": ""}
        detail = fetch_json(page, "GET", f"/api/videos/{video_id}") if video_id else {"status": 0, "body": {}}
        share_url = (((detail.get("body") or {}).get("video") or {}).get("share_url") or "").strip()
        shared_playback = {"status": 0, "body": {}}
        shared_master = {"status": 0, "text": ""}
        share_session_query = ""
        if share_url:
            token = share_url.rstrip("/").split("/")[-1]
            unlock = fetch_json(page, "POST", f"/api/videos/shared/{token}/unlock", {"password": "ShareDeep123!"})
            if unlock["status"] == 200:
                share_session_id = str((unlock.get("body") or {}).get("share_session_id") or "").strip()
                if share_session_id:
                    share_session_query = f"?share_session={share_session_id}"
                shared_playback = fetch_json(page, "GET", f"/api/videos/shared/{token}/playback{share_session_query}")
                shared_master_url = (shared_playback.get("body") or {}).get("master_url") or ""
                if shared_master_url:
                    shared_master = fetch_text(page, shared_master_url)
        playback_body = playback.get("body") or {}
        shared_playback_body = shared_playback.get("body") or {}
        playback_mode = str(playback_body.get("mode") or playback_body.get("recommended_mode") or "").strip()
        shared_playback_mode = str(shared_playback_body.get("mode") or shared_playback_body.get("recommended_mode") or "").strip()

        def realtime_or_direct_stream_url(body: dict[str, Any]) -> str:
            realtime_proxy = body.get("realtime_proxy") if isinstance(body, dict) else {}
            if not isinstance(realtime_proxy, dict):
                realtime_proxy = {}
            return str(
                body.get("stream_url")
                or body.get("realtime_proxy_url")
                or realtime_proxy.get("url")
                or ""
            ).strip()

        hls_master_ok = (
            playback["status"] == 200
            and playback_mode == "hls"
            and master["status"] == 200
            and 'CODECS="h264"' not in master.get("text", "")
            and "avc1." in master.get("text", "")
        )
        direct_or_realtime_ok = (
            playback["status"] == 200
            and playback_mode in {"direct", "realtime", "realtime_proxy"}
            and bool(realtime_or_direct_stream_url(playback_body))
            and not playback_body.get("master_url")
        )
        shared_hls_ok = (
            not share_url
            or (
                shared_playback["status"] == 200
                and shared_playback_mode == "hls"
                and shared_playback_body.get("hls_js_url") == "/js/hls.light.min.js?v=20260505-hlsjs"
                and shared_master["status"] == 200
                and 'CODECS="h264"' not in shared_master.get("text", "")
                and "avc1." in shared_master.get("text", "")
            )
        )
        shared_direct_or_realtime_ok = (
            not share_url
            or (
                shared_playback["status"] == 200
                and shared_playback_mode in {"direct", "realtime", "realtime_proxy"}
                and bool(realtime_or_direct_stream_url(shared_playback_body))
                and not shared_playback_body.get("master_url")
            )
        )
        playback_ok = hls_master_ok or direct_or_realtime_ok
        shared_playback_ok = shared_hls_ok or shared_direct_or_realtime_ok
        ok = bool(progress_seen) and bool(upload_success) and videos["status"] == 200 and playback_ok and shared_playback_ok
        rec.add(
            "video_upload_share_flow",
            ok,
            f"ui_progress={progress_seen}, progress={progress_percent}, list={videos['status']}, mode={playback_mode or 'unknown'}, shared_mode={shared_playback_mode or 'none'}",
            progress_seen=progress_seen,
            progress_status=progress_status,
            progress_percent=progress_percent,
            video_msg=video_msg,
            upload_success=upload_success,
            video_count=len(video_items),
            video_id=video_id,
            playback=playback.get("body"),
            master_status=master.get("status"),
            share_session_present=bool(share_session_query),
            shared_playback=shared_playback.get("body"),
            shared_master_status=shared_master.get("status"),
            latest_video=latest,
        )
        return {"video_id": video_id or latest.get("id"), "progress_seen": progress_seen}
    finally:
        try:
            video_path.unlink()
        except FileNotFoundError:
            pass


def check_economy_trading_journey(rec: Recorder, page, base_url: str) -> None:
    wallet = fetch_json(page, "GET", "/api/points/wallet")
    ledger = fetch_json(page, "GET", "/api/points/ledger")
    markets = fetch_json(page, "GET", "/api/trading/markets")
    market_rows = markets.get("body", {}).get("markets") or []
    market = market_rows[0] if market_rows else {}
    order_status = 0
    order_body: dict[str, Any] = {}
    if market.get("symbol"):
        order = fetch_json(
            page,
            "POST",
            "/api/trading/orders",
            {
                "market_symbol": market["symbol"],
                "side": "buy",
                "order_type": "limit",
                "quantity": "0.0001",
                "limit_price_points": "1",
            },
        )
        order_status = int(order["status"])
        order_body = order.get("body") or {}
    page.goto(base_url + "/", wait_until="domcontentloaded")
    wait_for_auth_app(page)
    switch_module(page, "economy")
    economy_ready = bool(page.evaluate("""() => {
        const module = document.querySelector('#module-economy');
        const refresh = document.querySelector('#economy-refresh-btn');
        return !!(module && module.classList.contains('active') && refresh);
    }"""))
    economy_visible = page.locator("#economy-refresh-btn").is_visible(timeout=1000)
    if economy_visible:
        page.click("#economy-refresh-btn")
    else:
        page.evaluate("() => document.querySelector('#economy-refresh-btn')?.click()")
    page.wait_for_timeout(700)
    check_ui_quality(rec, page, "economy_desktop")
    switch_module(page, "trading")
    trading_ready = bool(page.evaluate("""() => {
        const module = document.querySelector('#module-trading');
        const refresh = document.querySelector('#trading-refresh-btn');
        return !!(module && module.classList.contains('active') && refresh);
    }"""))
    trading_visible = page.locator("#trading-refresh-btn").is_visible(timeout=1000)
    if trading_visible:
        page.click("#trading-refresh-btn")
    else:
        page.evaluate("() => document.querySelector('#trading-refresh-btn')?.click()")
    page.wait_for_timeout(900)
    check_ui_quality(rec, page, "trading_desktop")
    ok = wallet["status"] == 200 and ledger["status"] == 200 and markets["status"] == 200 and order_status < 500 and economy_ready and trading_ready
    rec.add(
        "economy_trading_wallet_order_flow",
        ok,
        f"wallet={wallet['status']}, ledger={ledger['status']}, markets={markets['status']}, order={order_status}, economy_ready={economy_ready}, trading_ready={trading_ready}",
        market=market,
        order=order_body,
        economy_ready=economy_ready,
        economy_visible=economy_visible,
        trading_ready=trading_ready,
        trading_visible=trading_visible,
    )


def check_games_journey(rec: Recorder, page) -> None:
    catalog = fetch_json(page, "GET", "/api/games/catalog")
    games = (catalog.get("body") or {}).get("games") or []
    chess_game = next((item for item in games if item.get("key") == "chess"), {})
    difficulties = chess_game.get("computer_difficulties") or []
    difficulty = (difficulties[0] or {}).get("key") if difficulties else "experiment 0:minimax2ply"
    created = fetch_json(page, "POST", "/api/games/chess/practice", {"difficulty": difficulty, "side": "white"})
    match_id = (created.get("body") or {}).get("match_id")
    detail_status = 0
    if match_id:
        detail = fetch_json(page, "GET", f"/api/games/chess/matches/{int(match_id)}")
        detail_status = int(detail["status"])
    solo = fetch_json(
        page,
        "POST",
        "/api/games/minesweeper/solo-scores",
        {
            "score": 100,
            "raw_elapsed_ms": 1000,
            "elapsed_ms": 1000,
            "penalty_seconds": 0,
            "difficulty": "easy",
            "puzzle_id": "playwright-qa",
        },
    )
    switch_module(page, "games")
    page.click("#game-refresh-btn")
    page.wait_for_timeout(900)
    check_ui_quality(rec, page, "games_desktop")
    ok = catalog["status"] == 200 and created["status"] == 200 and detail_status == 200 and solo["status"] < 500
    rec.add(
        "games_catalog_chess_solo_flow",
        ok,
        f"catalog={catalog['status']}, chess={created['status']}, detail={detail_status}, solo={solo['status']}",
        match_id=match_id,
        difficulty=difficulty,
        created=created.get("body"),
        solo=solo.get("body"),
    )


def check_launch_security_journey(rec: Recorder, page) -> None:
    health = fetch_json(page, "GET", "/api/admin/health")
    readiness = fetch_json(page, "GET", "/api/admin/health/readiness")
    requirements = fetch_json(page, "GET", "/api/root/server-mode/requirements")
    production_status = fetch_json(page, "GET", "/api/root/production-report/status")
    doc = fetch_json(page, "GET", "/api/root/launch-check/doc?path=docs/server_mode_v2/03_production_gate_playbook.md")
    switch_server_tab(page, "server-mode")
    page.evaluate(
        """() => {
            const select = document.getElementById('server-mode-select');
            if (select) {
                select.value = 'production';
                select.dispatchEvent(new Event('change', { bubbles: true }));
            }
            if (typeof updateServerModeLaunchCheckVisibility === 'function') updateServerModeLaunchCheckVisibility();
            if (typeof loadLaunchCheck === 'function') loadLaunchCheck();
        }"""
    )
    page.wait_for_selector("#launch-check-list:visible", timeout=10000)
    check_ui_quality(rec, page, "launch_check_desktop")
    switch_server_tab(page, "health")
    check_ui_quality(rec, page, "security_health_desktop")
    ok = all(item["status"] == 200 for item in (health, readiness, requirements, production_status, doc))
    rec.add(
        "security_launch_check_flow",
        ok,
        f"health={health['status']}, readiness={readiness['status']}, requirements={requirements['status']}, reports={production_status['status']}, doc={doc['status']}",
        requirements=requirements.get("body"),
        production_status=production_status.get("body"),
    )


def check_comfyui_workflow_builder_flow(rec: Recorder, page) -> None:
    workflow = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "v1-5-pruned.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a cat sitting in a window", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "low quality", "clip": ["4", 1]}},
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": 42,
                "steps": 20,
                "cfg": 7.5,
                "denoise": 1.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "ComfyUI", "images": ["8", 0]}},
    }
    switch_module(page, "comfyui")
    if page.locator('[data-comfyui-view="workflow"]').count():
        page.click('[data-comfyui-view="workflow"]')
        page.wait_for_selector('[data-comfyui-view-panel="workflow"]:not([hidden])', timeout=10000)
    page.fill("#comfyui-workflow-title", f"QA Workflow {utc_stamp()}")
    page.fill("#comfyui-workflow-description", "Playwright 建立的 workflow layout builder 測試。")
    page.select_option("#comfyui-workflow-purpose", "txt2img")
    export_result = fetch_json(
        page,
        "POST",
        "/api/comfyui/workflow-layouts/export-current",
        {
            "workflow_json": {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "missing.safetensors"}}},
            "layout_json": {"nodes": [], "edges": []},
        },
    )
    create_result = fetch_json(
        page,
        "POST",
        "/api/comfyui/workflow-layouts",
        {
            "name": f"QA Workflow {utc_stamp()}",
            "description": "Playwright workflow builder CRUD",
            "purpose": "txt2img",
            "workflow_json": workflow,
            "layout_json": {"nodes": [{"id": "1", "type": "CheckpointLoaderSimple", "x": 10, "y": 10}], "edges": []},
            "required_models": ["v1-5-pruned.safetensors"],
            "required_custom_nodes": [],
            "visibility": "private",
        },
    )
    layouts = fetch_json(page, "GET", "/api/comfyui/workflow-layouts")
    page.wait_for_timeout(900)
    check_ui_quality(rec, page, "comfyui_builder_desktop")
    ok = export_result["status"] < 500 and create_result["status"] in {200, 201} and layouts["status"] == 200
    rec.add(
        "comfyui_workflow_builder_crud_flow",
        ok,
        f"export={export_result['status']}, create={create_result['status']}, list={layouts['status']}",
        export=export_result.get("body"),
        create=create_result.get("body"),
    )


def check_module_tabs(rec: Recorder, page, base_url: str, viewport: dict[str, int]) -> None:
    page.set_viewport_size(viewport)
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    tabs = [
        ("chat", "#tab-module-chat", "#module-chat"),
        ("announcements", "#tab-module-announcements", "#module-announcements"),
        ("community", "#tab-module-community", "#module-community"),
        ("drive", "#tab-module-drive", "#module-drive"),
        ("albums", "#tab-module-albums", "#module-albums"),
        ("videos", "#tab-module-videos", "#module-videos"),
        ("games", "#tab-module-games", "#module-games"),
        ("comfyui", "#tab-module-comfyui", "#module-comfyui"),
        ("economy", "#tab-module-economy", "#module-economy"),
        ("trading", "#tab-module-trading", "#module-trading"),
        ("appeals", "#tab-module-appeals", "#module-appeals"),
        ("accounts", "#tab-module-accounts", "#module-accounts"),
        ("server", "#tab-module-server", "#module-server"),
    ]
    failures: list[str] = []
    visited: list[str] = []
    for label, tab_sel, section_sel in tabs:
        if not page.locator(tab_sel).count():
            continue
        visible = page.locator(tab_sel).evaluate("el => getComputedStyle(el).display !== 'none' && !el.hidden")
        if not visible:
            continue
        try:
            page.evaluate("tab => { if (typeof switchModuleTab !== 'function') throw new Error('switchModuleTab missing'); switchModuleTab(tab); }", label)
            page.wait_for_timeout(350)
            active = page.locator(section_sel).evaluate("el => el.classList.contains('active')")
            overflow = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            if not active:
                failures.append(f"{label}: section not active")
            if overflow > 6:
                failures.append(f"{label}: horizontal overflow {overflow}px")
            visited.append(label)
        except Exception as exc:
            failures.append(f"{label}: {type(exc).__name__}: {str(exc)[:240]}")
    rec.add(
        f"module_tabs_{viewport['width']}x{viewport['height']}",
        not failures,
        ", ".join(failures) or f"visited {', '.join(visited)}",
        visited=visited,
        failures=failures,
    )


def check_comfyui_editor(rec: Recorder, page, base_url: str) -> None:
    page.goto(base_url + "/comfyui-workflow-editor.html", wait_until="domcontentloaded")
    page.wait_for_selector(".wf-node", state="attached", timeout=10000)
    node_count = page.locator(".wf-node").count()
    edge_count = page.locator(".edge-path").count()
    before_box = page.locator(".wf-node").first.bounding_box()
    before_path = page.locator(".edge-path").first.get_attribute("d") if edge_count else ""
    if not before_box:
        raise RuntimeError("workflow node has no bounding box")
    page.mouse.move(before_box["x"] + 20, before_box["y"] + 20)
    page.mouse.down()
    page.mouse.move(before_box["x"] + 115, before_box["y"] + 95, steps=8)
    page.mouse.up()
    page.wait_for_timeout(250)
    after_box = page.locator(".wf-node").first.bounding_box()
    after_path = page.locator(".edge-path").first.get_attribute("d") if edge_count else ""
    moved = bool(after_box and (abs(after_box["x"] - before_box["x"]) > 20 or abs(after_box["y"] - before_box["y"]) > 20))
    edge_updated = bool(before_path != after_path) if edge_count else True
    page.click("#autoLayoutBtn")
    page.wait_for_timeout(250)
    rec.add(
        "comfyui_visual_editor_drag_edges",
        node_count >= 3 and moved and edge_updated,
        f"nodes={node_count}, edges={edge_count}, moved={moved}, edge_updated={edge_updated}",
        nodes=node_count,
        edges=edge_count,
        moved=moved,
        edge_updated=edge_updated,
    )
    page.click("#sendBackBtn")
    page.wait_for_timeout(500)
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        "() => document.body.classList.contains('app-authenticated') && typeof switchModuleTab === 'function'",
        timeout=10000,
    )
    page.evaluate("() => { if (typeof switchModuleTab === 'function') switchModuleTab('comfyui'); }")
    page.wait_for_timeout(500)
    if page.locator('[data-comfyui-view="workflow"]').count():
        page.click('[data-comfyui-view="workflow"]')
        page.wait_for_selector('[data-comfyui-view-panel="workflow"]:not([hidden])', timeout=10000)
    load_btn = page.locator("#comfyui-workflow-load-visual-btn")
    if load_btn.count() == 1 and not load_btn.is_visible():
        page.evaluate(
            """() => {
                const btn = document.querySelector('#comfyui-workflow-load-visual-btn');
                const details = btn?.closest('details');
                if (details) details.open = true;
            }"""
        )
        page.wait_for_timeout(150)
    rec.add(
        "comfyui_main_page_visual_button",
        load_btn.count() == 1 and load_btn.is_visible(),
        "visual workflow button accessible from workflow action menu",
    )


def check_civitai_guard(rec: Recorder, page) -> None:
    result = fetch_json(
        page,
        "POST",
        "/api/root/comfyui/civitai/search",
        {"query": "test", "model_type": "checkpoint", "limit": 5, "source": "all"},
    )
    body = result.get("body") or {}
    msg = str(body.get("msg") or body.get("error") or "")
    ok = result["status"] in {400, 422} and ("API" in msg or "Civitai" in msg or "Key" in msg)
    rec.add("civitai_missing_key_guard", ok, f"status={result['status']}, msg={msg[:120]}", response=body)


def check_civitai_live_search(rec: Recorder, page, cfg: OptionalComfyUIConfig) -> None:
    if not cfg.has_live_civitai():
        check_civitai_guard(rec, page)
        return
    result = fetch_json(
        page,
        "POST",
        "/api/root/comfyui/civitai/search",
        {
            "query": cfg.civitai_query,
            "model_type": cfg.civitai_model_type,
            "limit": 8,
            "source": cfg.civitai_source,
            "nsfw_mode": "safe",
        },
    )
    body = result.get("body") or {}
    items = body.get("results") if isinstance(body.get("results"), list) else []
    wanted = cfg.civitai_model_type.lower()
    mismatches: list[str] = []
    missing_preview: list[str] = []
    missing_url: list[str] = []
    source_sites: set[str] = set()
    attempted_sources = {
        str(item.get("source_site") or "").strip()
        for item in list(body.get("search_sources") or []) + list(body.get("source_errors") or [])
        if isinstance(item, dict)
    }
    source_errors = [
        f"{item.get('source_site')}: {item.get('error')}"
        for item in (body.get("source_errors") or [])
        if isinstance(item, dict)
    ]
    for item in items:
        title = str(item.get("name") or item.get("title") or item.get("model_id") or "?")
        source_sites.add(str(item.get("source_site") or item.get("source_label") or ""))
        item_type = str(item.get("suggested_model_type") or item.get("type") or "").lower()
        if wanted and item_type and item_type != wanted.lower():
            mismatches.append(f"{title}:{item_type}")
        if not (item.get("thumbnail_proxy_url") or item.get("thumbnail_url")):
            missing_preview.append(title)
        if not (item.get("selected_page_url") or item.get("page_url")):
            missing_url.append(title)
    thumbnail_probe = {"status": 0, "content_type": "", "url": ""}
    first_thumb = ""
    for item in items:
        first_thumb = str(item.get("thumbnail_proxy_url") or item.get("thumbnail_url") or "")
        if first_thumb:
            break
    if first_thumb:
        thumbnail_probe = page.evaluate(
            """async url => {
                try {
                    const response = await fetch(url, {credentials: 'same-origin'});
                    return {
                        status: response.status,
                        content_type: response.headers.get('content-type') || '',
                        url,
                    };
                } catch (err) {
                    return {status: 0, content_type: '', url, error: String(err)};
                }
            }""",
            first_thumb,
        )
    expected_sources_ok = True
    if cfg.civitai_source.lower() == "all":
        expected_sources_ok = {"civitai.com", "civitai.red"}.issubset(attempted_sources)
    thumb_probe_ok = not first_thumb or (
        int(thumbnail_probe.get("status") or 0) == 200
        and str(thumbnail_probe.get("content_type") or "").lower().startswith("image/")
    )
    ok = (
        result["status"] == 200
        and body.get("ok")
        and bool(items)
        and not mismatches
        and not missing_preview
        and not missing_url
        and not source_errors
        and expected_sources_ok
        and thumb_probe_ok
    )
    detail = (
        f"items={len(items)}, sources={','.join(sorted(filter(None, source_sites))) or '-'}, "
        f"attempted={','.join(sorted(filter(None, attempted_sources))) or '-'}, "
        f"mismatches={len(mismatches)}, no_thumb={len(missing_preview)}, no_url={len(missing_url)}, "
        f"source_errors={len(source_errors)}, thumb_status={thumbnail_probe.get('status')}"
    )
    rec.add(
        "civitai_live_search",
        ok,
        detail,
        status=result["status"],
        item_count=len(items),
        mismatches=mismatches[:10],
        missing_preview=missing_preview[:10],
        missing_url=missing_url[:10],
        source_sites=sorted(filter(None, source_sites)),
        attempted_sources=sorted(filter(None, attempted_sources)),
        source_errors=source_errors[:10],
        thumbnail_probe=thumbnail_probe,
    )


def board_from_match(match: dict[str, Any]) -> chess.Board:
    state = match.get("board") or {}
    fen = state.get("__fen__")
    if fen:
        board = chess.Board(fen)
        board.turn = chess.WHITE if match.get("current_turn") == "white" else chess.BLACK
        return board
    board = chess.Board(None)
    for square, symbol in state.items():
        if square == "__fen__":
            continue
        try:
            board.set_piece_at(chess.parse_square(square), chess.Piece.from_symbol(symbol))
        except Exception:
            continue
    board.turn = chess.WHITE if match.get("current_turn") == "white" else chess.BLACK
    board.clear_stack()
    return board


PIECE_VALUES = {
    chess.PAWN: 100,
    chess.KNIGHT: 320,
    chess.BISHOP: 330,
    chess.ROOK: 500,
    chess.QUEEN: 900,
    chess.KING: 0,
}


def material_score(board: chess.Board) -> int:
    score = 0
    for piece in board.piece_map().values():
        value = PIECE_VALUES[piece.piece_type]
        score += value if piece.color == chess.WHITE else -value
    return score


def choose_human_move(board: chess.Board) -> chess.Move:
    legal = list(board.legal_moves)
    if not legal:
        raise RuntimeError("no legal moves")
    best: tuple[int, str, chess.Move] | None = None
    for move in legal:
        after = board.copy(stack=False)
        moving_piece = board.piece_at(move.from_square)
        captured = board.piece_at(move.to_square)
        if board.is_en_passant(move):
            captured = chess.Piece(chess.PAWN, not board.turn)
        after.push(move)
        score = 0
        if after.is_checkmate():
            score += 100000
        if captured:
            mover_value = PIECE_VALUES[moving_piece.piece_type] if moving_piece else 100
            score += 10 * PIECE_VALUES[captured.piece_type] - mover_value
        if after.is_check():
            score += 80
        if move.promotion:
            score += PIECE_VALUES.get(move.promotion, 0)
        to_file = chess.square_file(move.to_square)
        to_rank = chess.square_rank(move.to_square)
        score += 12 - (abs(to_file - 3.5) + abs(to_rank - 3.5)) * 2
        score += material_score(after) if board.turn == chess.WHITE else -material_score(after)
        key = (int(score), move.uci(), move)
        if best is None or key > best:
            best = key
    return best[2]


def play_exp4_chess(rec: Recorder, page, max_human_moves: int) -> dict[str, Any]:
    created = fetch_json(page, "POST", "/api/games/chess/practice", {"difficulty": "experiment 4:pv", "side": "white"})
    if created["status"] != 200 or not created["body"].get("ok"):
        raise RuntimeError(f"practice create failed: {created}")
    match_id = int(created["body"]["match_id"])
    detail = fetch_json(page, "GET", f"/api/games/chess/matches/{match_id}")
    match = detail["body"]["match"]
    moves: list[str] = []
    adjudicated = False
    winner = "unknown"
    reason = ""
    for _ in range(max_human_moves):
        if match.get("status") != "active":
            break
        board = board_from_match(match)
        move = choose_human_move(board)
        payload = {"from": chess.square_name(move.from_square), "to": chess.square_name(move.to_square)}
        if move.promotion:
            payload["promotion"] = chess.piece_symbol(move.promotion)
        moved = fetch_json(page, "POST", f"/api/games/chess/matches/{match_id}/move", payload)
        if moved["status"] != 200 or not moved["body"].get("ok"):
            raise RuntimeError(f"move {move.uci()} failed: {moved}")
        moves.append(move.uci())
        match = moved["body"]["match"]
    if match.get("status") == "active":
        adjudicated = True
        board = board_from_match(match)
        score = material_score(board)
        if score > 150:
            winner = "human"
            reason = f"adjudicated_material_white_plus_{score}"
        elif score < -150:
            winner = "experiment_4_pv"
            reason = f"adjudicated_material_black_plus_{abs(score)}"
        else:
            winner = "draw"
            reason = f"adjudicated_material_near_even_{score}"
    else:
        reason = str(match.get("result_reason") or "finished")
        if match.get("winner_user_id"):
            winner = "human"
        elif reason in {"stalemate", "insufficient_material", "seventyfive_moves", "fivefold_repetition", "draw"}:
            winner = "draw"
        else:
            winner = "experiment_4_pv"
    summary = {
        "match_id": match_id,
        "winner": winner,
        "reason": reason,
        "adjudicated": adjudicated,
        "human_moves": moves,
        "move_history": match.get("move_history", []),
        "status": match.get("status"),
        "result_reason": match.get("result_reason"),
    }
    rec.add("chess_vs_experiment_4_pv", winner in {"human", "experiment_4_pv", "draw"}, f"winner={winner}, reason={reason}", **summary)
    return summary


def write_reports(runtime_root: Path, stamp: str, summary: dict[str, Any]) -> tuple[Path, Path]:
    report_dir = runtime_root / "reports" / "qa"
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / f"playwright_deep_site_check_{stamp}.json"
    md_path = report_dir / f"playwright_deep_site_check_{stamp}.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    lines = [
        "# Playwright Deep Site Check",
        "",
        f"- Base URL: `{summary['base_url']}`",
        f"- Runtime root: `{summary['runtime_root']}`",
        f"- Started at: `{summary['started_at']}`",
        f"- Finished at: `{summary['finished_at']}`",
        f"- Chess winner: `{summary.get('chess', {}).get('winner', 'unknown')}`",
        f"- Chess reason: `{summary.get('chess', {}).get('reason', '')}`",
        "",
        "## Results",
        "",
    ]
    for item in summary["checks"]:
        mark = "PASS" if item["ok"] else "FAIL"
        lines.append(f"- `{mark}` **{item['name']}**: {item.get('detail', '')}")
    lines.extend(["", "## Console/Page Errors", ""])
    for err in summary["browser_errors"]:
        lines.append(f"- `{err['type']}` {err['text']}")
    if not summary["browser_errors"]:
        lines.append("- none")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", default="")
    parser.add_argument("--keep-server", action="store_true")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--max-chess-human-moves", type=int, default=40)
    parser.add_argument("--interactive-comfyui", action="store_true", help="Prompt for optional live ComfyUI/Civitai settings before running live checks.")
    parser.add_argument("--comfyui-api-url", default=os.environ.get("PLAYWRIGHT_COMFYUI_API_URL", "").strip(), help="Optional remote ComfyUI URL. Must be http(s)://host:port.")
    parser.add_argument("--comfyui-base-dir", default=os.environ.get("PLAYWRIGHT_COMFYUI_BASE_DIR", "").strip(), help="Optional local ComfyUI base directory for autostart checks.")
    parser.add_argument("--comfyui-start-script", default=os.environ.get("PLAYWRIGHT_COMFYUI_START_SCRIPT", "").strip(), help="Optional local ComfyUI start script name/path under the base directory.")
    parser.add_argument("--comfyui-api-host", default=os.environ.get("PLAYWRIGHT_COMFYUI_API_HOST", "").strip(), help="Optional local ComfyUI API host.")
    parser.add_argument("--comfyui-api-port", type=int, default=env_int("PLAYWRIGHT_COMFYUI_API_PORT"), help="Optional local ComfyUI API port.")
    parser.add_argument("--civitai-api-key", default=os.environ.get("PLAYWRIGHT_CIVITAI_API_KEY", os.environ.get("CIVITAI_API_KEY", "")).strip(), help="Optional Civitai API key for live search checks.")
    parser.add_argument("--civitai-live-query", default=os.environ.get("PLAYWRIGHT_CIVITAI_QUERY", "sdxl"))
    parser.add_argument("--civitai-live-model-type", default=os.environ.get("PLAYWRIGHT_CIVITAI_MODEL_TYPE", "checkpoint"))
    parser.add_argument("--civitai-live-source", default=os.environ.get("PLAYWRIGHT_CIVITAI_SOURCE", "all"))
    args = parser.parse_args()
    optional_comfyui = collect_optional_comfyui_config(args)

    stamp = utc_stamp()
    runtime_root = Path(args.runtime_root).resolve() if args.runtime_root else Path("/tmp") / f"hackme_web_playwright_deep_{stamp}"
    mkdirs(runtime_root)
    port = free_port()
    started_at = datetime.now(timezone.utc).isoformat()
    rec = Recorder()
    server = start_server(runtime_root, port)
    base_url = ""
    browser_errors: list[dict[str, str]] = []
    chess_summary: dict[str, Any] = {}
    try:
        base_url = wait_for_server(port)
        rec.add("server_start", True, base_url, pid=server.pid, runtime_root=str(runtime_root))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            context = browser.new_context(ignore_https_errors=True, viewport={"width": 1366, "height": 768})
            seen_browser_errors: set[str] = set()

            def record_browser_error(kind: str, text: str) -> None:
                compact = text.replace("\n", " ")[:500]
                key = f"{kind}:{compact}"
                if key in seen_browser_errors or len(browser_errors) >= 80:
                    return
                seen_browser_errors.add(key)
                browser_errors.append({"type": kind, "text": compact})

            def new_page(viewport: dict[str, int] | None = None):
                page = context.new_page()
                if viewport:
                    page.set_viewport_size(viewport)
                attach_browser_error_handlers(page, record_browser_error)
                return page

            page = new_page({"width": 1366, "height": 768})

            def unauth_editor() -> None:
                anon = browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 720})
                anon_page = anon.new_page()
                anon_page.goto(base_url + "/comfyui-workflow-editor.html", wait_until="domcontentloaded")
                final_url = anon_page.url
                anon.close()
                if "/comfyui-workflow-editor.html" in final_url:
                    raise RuntimeError(f"unauthenticated editor remained accessible: {final_url}")

            rec.guard("protected_comfyui_editor_requires_login", unauth_editor)
            rec.guard("ui_login_root", lambda: login(page, base_url))
            rec.guard("enable_required_features", lambda: enable_required_features(page, base_url))
            rec.guard("optional_comfyui_settings", lambda: apply_optional_comfyui_settings(rec, page, optional_comfyui))
            rec.guard("api_surface", lambda: check_api_surface(rec, page))
            rec.guard("auth_registration_journey", lambda: check_auth_registration_journey(rec, browser, base_url, page))
            rec.guard("admin_member_management_journey", lambda: check_admin_member_management(rec, page))
            rec.guard("forum_journey", lambda: check_forum_journey(rec, page))
            rec.guard("drive_e2ee_journey", lambda: check_drive_e2ee_journey(rec, page))
            rec.guard("video_share_journey", lambda: check_video_share_journey(rec, page))
            rec.guard("games_journey", lambda: check_games_journey(rec, page))
            rec.guard("economy_trading_journey", lambda: check_economy_trading_journey(rec, page, base_url))
            rec.guard("launch_security_journey", lambda: check_launch_security_journey(rec, page))
            rec.guard("comfyui_workflow_builder_journey", lambda: check_comfyui_workflow_builder_flow(rec, page))
            page.close()

            desktop_page = new_page({"width": 1366, "height": 768})
            rec.guard("module_tabs_desktop", lambda: check_module_tabs(rec, desktop_page, base_url, {"width": 1366, "height": 768}))
            desktop_page.close()

            mobile_page = new_page({"width": 390, "height": 844})
            rec.guard("module_tabs_mobile", lambda: check_module_tabs(rec, mobile_page, base_url, {"width": 390, "height": 844}))
            mobile_page.close()

            editor_page = new_page({"width": 1366, "height": 768})
            rec.guard("comfyui_editor", lambda: check_comfyui_editor(rec, editor_page, base_url))
            editor_page.close()

            api_page = new_page({"width": 1366, "height": 768})
            api_page.goto(base_url + "/", wait_until="domcontentloaded")
            rec.guard("comfyui_live_connection_optional", lambda: check_live_comfyui_connection(rec, api_page, optional_comfyui))
            rec.guard("civitai_search", lambda: check_civitai_live_search(rec, api_page, optional_comfyui))
            chess_summary = rec.guard("play_chess_exp4", lambda: play_exp4_chess(rec, api_page, args.max_chess_human_moves)) or {}
            api_page.close()
            browser.close()
    finally:
        if server.poll() is None and not args.keep_server:
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)

    finished_at = datetime.now(timezone.utc).isoformat()
    checks = [{"name": r.name, "ok": r.ok, "detail": r.detail, **({"data": r.data} if r.data else {})} for r in rec.results]
    summary = {
        "ok": all(item["ok"] for item in checks),
        "started_at": started_at,
        "finished_at": finished_at,
        "runtime_root": str(runtime_root),
        "base_url": base_url,
        "checks": checks,
        "browser_errors": browser_errors,
        "chess": chess_summary,
        "optional_comfyui": optional_comfyui.safe_summary(),
    }
    json_path, md_path = write_reports(runtime_root, stamp, summary)
    summary["json_report"] = str(json_path)
    summary["markdown_report"] = str(md_path)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["ok"] and not browser_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
