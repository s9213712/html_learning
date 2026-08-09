#!/usr/bin/env python3
"""Exercise the product's real Diffusers img2img route and retain reviewable output.

This is deliberately separate from the high-concurrency request rotor: a
Diffusers edit owns a model and can run for minutes, so treating a queued HTTP
200 as successful would be misleading.  Each iteration waits for the product
job to complete, verifies the backend identity, downloads the generated image,
and records an artifact for human visual review.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


DEFAULT_MODEL = "dhead/wai-nsfw-illustrious-sdxl-v140-sdxl"
DEFAULT_PROMPT = (
    "an adult fully clothed person; preserve the blonde hair, white shirt, "
    "blue shorts, full-body standing pose, and composition; improve clean "
    "natural lighting and background detail"
)
DEFAULT_NEGATIVE = "child, minor, underage, nude, naked, explicit, watermark, text, blurry, distorted anatomy"


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_error(response: requests.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("msg") or body.get("error") or body)[:800]
    except Exception:
        pass
    return (response.text or "")[:800]


def _decode_data_url(value: str) -> bytes:
    prefix, marker, encoded = str(value or "").partition(",")
    if not marker or not prefix.startswith("data:") or ";base64" not in prefix.lower():
        raise ValueError("image preview did not include a base64 data URL")
    return base64.b64decode(encoded, validate=True)


def _image_shape_and_nonblank(path: Path) -> dict[str, Any]:
    from PIL import Image, ImageStat

    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        stats = ImageStat.Stat(rgb)
        extrema = rgb.getextrema()
        nonblank = any(low != high for low, high in extrema)
        return {
            "width": int(rgb.width),
            "height": int(rgb.height),
            "mode": rgb.mode,
            "mean": [round(float(value), 3) for value in stats.mean],
            "nonblank": bool(nonblank),
        }


@dataclass
class ProductClient:
    base_url: str
    username: str
    password: str
    timeout_seconds: float

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")
        self.session = requests.Session()
        self.session.verify = False
        self.csrf = ""

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            headers.setdefault("X-CSRF-Token", self.csrf)
        response = self.session.request(
            method.upper(),
            f"{self.base_url}{path}",
            headers=headers,
            timeout=self.timeout_seconds,
            **kwargs,
        )
        rotated = self.session.cookies.get("csrf_token")
        if rotated:
            self.csrf = str(rotated)
        return response

    def login(self) -> None:
        csrf_response = self.session.get(
            f"{self.base_url}/api/csrf-token", timeout=self.timeout_seconds
        )
        if csrf_response.status_code != 200:
            raise RuntimeError(f"csrf bootstrap failed: HTTP {csrf_response.status_code}: {_json_error(csrf_response)}")
        self.csrf = str((csrf_response.json() or {}).get("csrf_token") or "")
        response = self._request(
            "POST",
            "/api/login",
            json={"username": self.username, "password": self.password},
        )
        if response.status_code != 200:
            raise RuntimeError(f"login failed: HTTP {response.status_code}: {_json_error(response)}")
        csrf_response = self.session.get(
            f"{self.base_url}/api/csrf-token", timeout=self.timeout_seconds
        )
        if csrf_response.status_code == 200:
            self.csrf = str((csrf_response.json() or {}).get("csrf_token") or self.csrf)

    def submit_i2i(self, *, source: Path, backend_url: str, model: str, variant: str, prompt: str, negative_prompt: str, width: int, height: int, steps: int, strength: float, job_timeout_seconds: int) -> str:
        mime_type = mimetypes.guess_type(source.name)[0] or "image/png"
        with source.open("rb") as image_file:
            response = self._request(
                "POST",
                "/api/comfyui/generate",
                files={"source_image": (source.name, image_file, mime_type)},
                data={
                    "backend_url": backend_url,
                    "generation_mode": "img2img",
                    "model": model,
                    "diffusers_model_repo": model,
                    "diffusers_model_variant": variant,
                    "prompt": prompt,
                    "negative_prompt": negative_prompt,
                    "edit_instruction": "Preserve the source identity and composition while applying the requested gentle visual edit.",
                    "width": str(width),
                    "height": str(height),
                    "steps": str(steps),
                    "cfg": "5",
                    "denoise_strength": str(strength),
                    "batch_size": "1",
                    "confirm_billing": "true",
                    "timeout_seconds": str(job_timeout_seconds),
                },
            )
        if response.status_code != 200:
            raise RuntimeError(f"img2img submission failed: HTTP {response.status_code}: {_json_error(response)}")
        body = response.json() if response.content else {}
        job_id = str(((body or {}).get("job") or {}).get("job_id") or "").strip()
        if not job_id:
            raise RuntimeError("img2img submission returned no job_id")
        return job_id

    def wait_for_job(self, job_id: str, *, timeout_seconds: int, poll_seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        last_job: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = self._request("GET", f"/api/comfyui/jobs/{job_id}")
            if response.status_code != 200:
                raise RuntimeError(f"img2img job lookup failed: HTTP {response.status_code}: {_json_error(response)}")
            body = response.json() if response.content else {}
            job = (body or {}).get("job") or {}
            if not isinstance(job, dict):
                raise RuntimeError("img2img job lookup returned malformed payload")
            last_job = job
            status = str(job.get("status") or "").lower()
            if status == "completed":
                progress = job.get("progress") or {}
                if str(progress.get("backend_kind") or "").lower() != "diffusers":
                    raise RuntimeError(f"job completed through unexpected backend: {progress.get('backend_kind')!r}")
                result = job.get("result") or {}
                if not isinstance(result, dict) or not isinstance(result.get("image"), dict):
                    raise RuntimeError("completed Diffusers job returned no image result")
                return job
            if status in {"error", "cancelled", "canceled"}:
                raise RuntimeError(str(job.get("error") or "Diffusers img2img job failed"))
            time.sleep(max(0.2, float(poll_seconds)))
        raise RuntimeError(f"img2img job timed out after {timeout_seconds}s; last status={last_job.get('status')!r}")

    def fetch_preview(self, image_ref: dict[str, Any]) -> bytes:
        response = self._request("POST", "/api/comfyui/image-preview", json={"image_ref": image_ref})
        if response.status_code != 200:
            raise RuntimeError(f"img2img preview failed: HTTP {response.status_code}: {_json_error(response)}")
        body = response.json() if response.content else {}
        data_url = str((((body or {}).get("image") or {}).get("data_url") or ""))
        return _decode_data_url(data_url)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real product HF/Diffusers img2img edits and retain reviewable artifacts.")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default=os.environ.get("HACKME_HF_I2I_PASSWORD", ""))
    parser.add_argument("--source-image", required=True)
    parser.add_argument("--backend-url", default=f"diffusers://local/{DEFAULT_MODEL}")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--variant", default="__default__", help="HF/Diffusers precision variant; use __default__ for the repository default weights")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--negative-prompt", default=DEFAULT_NEGATIVE)
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=640)
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--strength", type=float, default=0.25)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--interval-seconds", type=float, default=0.0)
    parser.add_argument("--job-timeout-seconds", type=int, default=600)
    parser.add_argument("--request-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--out", required=True)
    parser.add_argument("--artifacts-dir", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    requests.packages.urllib3.disable_warnings()
    try:
        ssl._create_default_https_context = ssl._create_unverified_context
    except Exception:
        pass
    source = Path(args.source_image).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    artifacts_dir = Path(args.artifacts_dir).expanduser().resolve() if args.artifacts_dir else out_path.parent / "hf_diffusers_i2i_artifacts"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "schema_version": "hackme.hf-diffusers-i2i-soak.v1",
        "ok": False,
        "started_at": _utc_now(),
        "base_url": args.base_url.rstrip("/"),
        "source_image": str(source),
        "backend_url": args.backend_url,
        "model": args.model,
        "runs_requested": max(1, int(args.runs)),
        "runs": [],
        "manual_visual_review_required": True,
    }
    if not args.password:
        report["error"] = "password is required via --password or HACKME_HF_I2I_PASSWORD"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if not source.is_file():
        report["error"] = "source image does not exist"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if args.width <= 0 or args.height <= 0 or args.width % 8 or args.height % 8:
        report["error"] = "width and height must be positive multiples of 8"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2
    if args.steps < 2 or not 0 < args.strength <= 1:
        report["error"] = "steps must be at least 2 and strength must be in (0, 1]"
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2

    client = ProductClient(args.base_url, args.username, args.password, max(1.0, float(args.request_timeout_seconds)))
    try:
        client.login()
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    else:
        for index in range(max(1, int(args.runs))):
            run: dict[str, Any] = {"index": index + 1, "started_at": _utc_now(), "ok": False}
            started = time.perf_counter()
            try:
                job_id = client.submit_i2i(
                    source=source,
                    backend_url=args.backend_url,
                    model=args.model,
                    variant=args.variant,
                    prompt=args.prompt,
                    negative_prompt=args.negative_prompt,
                    width=args.width,
                    height=args.height,
                    steps=args.steps,
                    strength=args.strength,
                    job_timeout_seconds=args.job_timeout_seconds,
                )
                job = client.wait_for_job(job_id, timeout_seconds=args.job_timeout_seconds, poll_seconds=args.poll_seconds)
                image = (job.get("result") or {}).get("image") or {}
                image_ref = image.get("image_ref") if isinstance(image, dict) else None
                if not isinstance(image_ref, dict):
                    raise RuntimeError("completed Diffusers job did not return a valid image_ref")
                output_path = artifacts_dir / f"hf_diffusers_i2i_{index + 1:03d}.png"
                output_path.write_bytes(client.fetch_preview(image_ref))
                inspection = _image_shape_and_nonblank(output_path)
                if not inspection["nonblank"]:
                    raise RuntimeError("generated image is visually blank")
                run.update({
                    "ok": True,
                    "job_id": job_id,
                    "backend_kind": (job.get("progress") or {}).get("backend_kind"),
                    "image_ref": image_ref,
                    "artifact": str(output_path),
                    "artifact_bytes": output_path.stat().st_size,
                    "inspection": inspection,
                })
            except Exception as exc:
                run["error"] = f"{type(exc).__name__}: {exc}"
            run["elapsed_seconds"] = round(time.perf_counter() - started, 3)
            run["finished_at"] = _utc_now()
            report["runs"].append(run)
            if index + 1 < max(1, int(args.runs)) and args.interval_seconds > 0:
                time.sleep(float(args.interval_seconds))
    report["finished_at"] = _utc_now()
    report["ok"] = bool(report["runs"]) and all(bool(item.get("ok")) for item in report["runs"])
    report["successful_runs"] = sum(1 for item in report["runs"] if item.get("ok"))
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
