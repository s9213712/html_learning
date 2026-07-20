#!/usr/bin/env python3
"""Real multi-user ComfyUI queue, interruption, and ownership audit.

This probe intentionally submits two real GPU jobs to a shared ComfyUI backend.
The second user then presses the existing global interrupt endpoint while their
own prompt is still pending.  Evidence is written without credentials.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import requests
import urllib3

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.testing.probe_credentials import (
    add_manager_password_argument,
    add_user_password_argument,
    redact_artifact_data,
)


urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ActorClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.verify = False
        self.csrf = ""

    def login(self) -> dict[str, Any]:
        csrf_response = self.session.get(f"{self.base_url}/api/csrf-token", timeout=15)
        csrf_response.raise_for_status()
        self.csrf = str(csrf_response.json().get("csrf_token") or "")
        response = self.session.post(
            f"{self.base_url}/api/login",
            json={"username": self.username, "password": self.password},
            headers={"X-CSRF-Token": self.csrf},
            timeout=15,
        )
        payload = _json(response)
        if response.status_code != 200 or not payload.get("ok"):
            raise RuntimeError(f"{self.username} login failed: HTTP {response.status_code}")
        self.csrf = str(self.session.cookies.get("csrf_token") or self.csrf)
        return {"status": response.status_code, "ok": True}

    def request(self, method: str, path: str, *, body: dict[str, Any] | None = None, timeout: int = 90):
        headers = {"X-CSRF-Token": self.csrf}
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            json=body,
            headers=headers,
            timeout=timeout,
        )
        return response, _json(response)


def _json(response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        payload = {"raw": str(response.text or "")[:1000]}
    return payload if isinstance(payload, dict) else {"value": payload}


def queue_snapshot(comfyui_url: str) -> dict[str, Any]:
    response = requests.get(f"{comfyui_url.rstrip('/')}/queue", timeout=30)
    response.raise_for_status()
    payload = response.json()

    def prompt_ids(name: str) -> list[str]:
        values = payload.get(name) if isinstance(payload, dict) else []
        return [str(item[1]) for item in (values or []) if isinstance(item, list) and len(item) > 1]

    return {
        "running": prompt_ids("queue_running"),
        "pending": prompt_ids("queue_pending"),
    }


def submit_job(client: ActorClient, *, model: str, prompt: str, steps: int, seed: int):
    response, payload = client.request(
        "POST",
        "/api/comfyui/generate",
        body={
            "model": model,
            "prompt": prompt,
            "negative_prompt": "text, watermark, logo, low quality, worst quality",
            "generation_mode": "txt2img",
            "width": 512,
            "height": 512,
            "steps": steps,
            "cfg": 6.5,
            "sampler_name": "euler",
            "scheduler": "normal",
            "seed": seed,
            "batch_size": 1,
            "timeout_seconds": 900,
            "confirm_billing": True,
        },
        timeout=120,
    )
    job = payload.get("job") if isinstance(payload.get("job"), dict) else {}
    if response.status_code != 200 or not payload.get("ok") or not job.get("job_id"):
        raise RuntimeError(
            f"{client.username} submit failed: HTTP {response.status_code} "
            f"{payload.get('msg') or payload.get('error') or ''}"
        )
    return str(job["job_id"]), {"status": response.status_code, "payload": payload}


def get_job(client: ActorClient, job_id: str):
    response, payload = client.request("GET", f"/api/comfyui/jobs/{job_id}", timeout=30)
    return response.status_code, payload.get("job") if isinstance(payload.get("job"), dict) else payload


def wait_for_prompt(client: ActorClient, job_id: str, timeout_seconds: int = 120):
    deadline = time.time() + timeout_seconds
    observations = []
    while time.time() < deadline:
        status, job = get_job(client, job_id)
        progress = job.get("progress") if isinstance(job, dict) and isinstance(job.get("progress"), dict) else {}
        observations.append({
            "http_status": status,
            "job_status": job.get("status") if isinstance(job, dict) else "",
            "phase": progress.get("phase"),
            "prompt_id": progress.get("prompt_id"),
            "queue_remaining": progress.get("queue_remaining"),
        })
        if progress.get("prompt_id"):
            return str(progress["prompt_id"]), observations
        if isinstance(job, dict) and job.get("status") in {"completed", "error", "cancelled"}:
            break
        time.sleep(0.5)
    return "", observations


def wait_terminal(client: ActorClient, job_id: str, timeout_seconds: int):
    deadline = time.time() + timeout_seconds
    observations = []
    last: dict[str, Any] = {}
    while time.time() < deadline:
        status, job = get_job(client, job_id)
        last = job if isinstance(job, dict) else {}
        progress = last.get("progress") if isinstance(last.get("progress"), dict) else {}
        sample = {
            "http_status": status,
            "job_status": last.get("status"),
            "phase": progress.get("phase"),
            "prompt_id": progress.get("prompt_id"),
            "queue_remaining": progress.get("queue_remaining"),
            "error": last.get("error") or progress.get("error_message"),
        }
        if not observations or sample != observations[-1]:
            observations.append(sample)
        if last.get("status") in {"completed", "error", "cancelled"}:
            return last, observations
        time.sleep(1)
    return {**last, "timed_out": True}, observations


def first_image_ref(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    images = result.get("images") if isinstance(result.get("images"), list) else []
    for item in images:
        ref = item.get("image_ref") if isinstance(item, dict) else None
        if isinstance(ref, dict) and ref.get("filename"):
            return ref
    return {}


def preview_check(client: ActorClient, image_ref: dict[str, Any], output_path: Path | None = None):
    response, payload = client.request("POST", "/api/comfyui/image-preview", body={"image_ref": image_ref})
    image = payload.get("image") if isinstance(payload.get("image"), dict) else {}
    if output_path and response.status_code == 200 and image.get("data_url"):
        encoded = str(image["data_url"]).split(",", 1)[-1]
        output_path.write_bytes(base64.b64decode(encoded))
    return {
        "status": response.status_code,
        "ok": bool(payload.get("ok")),
        "msg": payload.get("msg") or "",
        "size_bytes": image.get("size_bytes"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5027")
    parser.add_argument("--comfyui-url", default="http://127.0.0.1:8189")
    parser.add_argument("--first-user", default="admin")
    parser.add_argument("--second-user", default="test")
    add_manager_password_argument(parser)
    add_user_password_argument(parser)
    parser.add_argument("--model", default="JANKUTrainedChenkinNoobai_v777.safetensors")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"/tmp/hackme_comfyui_multi_user_{stamp}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    first = ActorClient(args.base_url, args.first_user, args.manager_password)
    second = ActorClient(args.base_url, args.second_user, args.test_password)
    report: dict[str, Any] = {
        "ok": False,
        "actors": [args.first_user, args.second_user],
        "model": args.model,
        "started_at": time.time(),
    }
    try:
        report["login"] = {args.first_user: first.login(), args.second_user: second.login()}
        first_job_id, first_submit = submit_job(
            first,
            model=args.model,
            prompt="multi-user isolation A, blue ceramic robot on a white table, no text",
            steps=40,
            seed=701001,
        )
        first_prompt_id, first_prompt_observations = wait_for_prompt(first, first_job_id)
        second_job_id, second_submit = submit_job(
            second,
            model=args.model,
            prompt="multi-user isolation B, red ceramic fox on a black table, no text",
            steps=12,
            seed=702002,
        )
        second_prompt_id, second_prompt_observations = wait_for_prompt(second, second_job_id)
        before_interrupt = queue_snapshot(args.comfyui_url)
        cross_first_status, _ = get_job(second, first_job_id)
        cross_second_status, _ = get_job(first, second_job_id)
        # Job-scoped cancellation must delete only the second user's pending prompt;
        # the legacy no-body endpoint is intentionally global/policy guarded.
        interrupt_response, interrupt_payload = second.request(
            "POST", "/api/comfyui/interrupt", body={"job_id": second_job_id}
        )
        time.sleep(1)
        after_interrupt = queue_snapshot(args.comfyui_url)
        first_job, first_terminal_observations = wait_terminal(first, first_job_id, args.timeout_seconds)
        second_job, second_terminal_observations = wait_terminal(second, second_job_id, args.timeout_seconds)
        first_ref = first_image_ref(first_job)
        second_ref = first_image_ref(second_job)
        previews = {
            "first_own": preview_check(first, first_ref, out_dir / "first_user_output.png") if first_ref else None,
            "second_own": preview_check(second, second_ref, out_dir / "second_user_output.png") if second_ref else None,
            "first_reads_second": preview_check(first, second_ref) if second_ref else None,
            "second_reads_first": preview_check(second, first_ref) if first_ref else None,
        }
        _, first_history = first.request("GET", "/api/comfyui/history")
        _, second_history = second.request("GET", "/api/comfyui/history")
        report.update({
            "jobs": {
                "first": {"job_id": first_job_id, "prompt_id": first_prompt_id, "submit": first_submit, "terminal": first_job},
                "second": {"job_id": second_job_id, "prompt_id": second_prompt_id, "submit": second_submit, "terminal": second_job},
            },
            "prompt_observations": {"first": first_prompt_observations, "second": second_prompt_observations},
            "terminal_observations": {"first": first_terminal_observations, "second": second_terminal_observations},
            "queue": {"before_interrupt": before_interrupt, "after_interrupt": after_interrupt},
            "interrupt": {"status": interrupt_response.status_code, "payload": interrupt_payload},
            "cross_job_status": {"second_reads_first": cross_first_status, "first_reads_second": cross_second_status},
            "previews": previews,
            "history_prompt_ids": {
                "first": [str((item.get("result") or {}).get("prompt_id") or "") for item in first_history.get("history") or []],
                "second": [str((item.get("result") or {}).get("prompt_id") or "") for item in second_history.get("history") or []],
            },
        })
        isolation_ok = (
            cross_first_status == 403
            and cross_second_status == 403
            and (not second_ref or previews["first_reads_second"]["status"] == 403)
            and (not first_ref or previews["second_reads_first"]["status"] == 403)
        )
        other_user_survived = first_job.get("status") == "completed"
        own_job_stopped = second_job.get("status") in {"cancelled", "error"}
        interrupt_targeted_own_job = bool(other_user_survived and own_job_stopped)
        report["verdict"] = {
            "job_owner_isolation_ok": isolation_ok,
            "other_user_job_survived_interrupt": other_user_survived,
            "own_job_stopped": own_job_stopped,
            "interrupt_targeted_own_job": interrupt_targeted_own_job,
            "first_status": first_job.get("status"),
            "second_status": second_job.get("status"),
        }
        report["ok"] = bool(isolation_ok and interrupt_targeted_own_job)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        report["finished_at"] = time.time()
        safe = redact_artifact_data(
            report,
            secret_values=(args.manager_password, args.test_password),
        )
        report_path = out_dir / "report.json"
        report_path.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": report.get("ok"), "report": str(report_path), "error": report.get("error", "")}, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
