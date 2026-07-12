#!/usr/bin/env python3
"""Isolated ComfyUI channel smoke test.

The script keeps generated reports/artifacts outside the repo and lets root
pin Hugging Face cache placement before running regular ComfyUI, Diffusers,
and GGUF channel checks.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import http.cookiejar
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.comfyui.client import ComfyUIClient  # noqa: E402


DEFAULT_REGULAR_MODEL = "SDXL\\illustrious(IL)\\janxd系列\\JANKUTrainedChenkinNoobai_v777.safetensors"
DEFAULT_HF_MODEL = "dhead/wai-nsfw-illustrious-sdxl-v140-sdxl"
DEFAULT_GGUF_MODEL = "kekusprod/WAI-NSFW-illustrious-SDXL-v110-GGUF"
DEFAULT_PROMPT = "adult women, fully clothed, by ogipote, 2girls, girls love, kiss, saliva, maid uniform, cat ears, cat tail"
DEFAULT_NEGATIVE = "child, minor, underage, loli, teen, nude, naked, explicit, low quality, blurry, watermark, distorted"


class ProbeError(RuntimeError):
    pass


class WebClient:
    def __init__(self, base_url: str, *, insecure: bool = False):
        self.base_url = str(base_url or "").rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if self.base_url.startswith("https://"):
            context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [("User-Agent", "hackme_web-comfyui-isolated-channel-smoke/1.0")]
        self.csrf_token = ""

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    @staticmethod
    def _read_body(resp) -> bytes:
        try:
            return resp.read()
        except http.client.IncompleteRead as exc:
            return exc.partial or b""

    def request_json(self, path: str, *, method="GET", payload=None, allow_http_error=False, timeout=60):
        body = None
        headers = {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            if self.csrf_token:
                headers["X-CSRF-Token"] = self.csrf_token
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._url(path), data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = self._read_body(resp)
                status = int(resp.status)
        except urllib.error.HTTPError as exc:
            raw = self._read_body(exc)
            status = int(exc.code)
            if not allow_http_error:
                raise
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            raise ProbeError(f"{method} {path} returned non-JSON HTTP {status}") from exc
        if not isinstance(data, dict):
            data = {"ok": False, "raw": data}
        data["_http_status"] = status
        return data

    def fetch_csrf(self):
        payload = self.request_json("/api/csrf-token", timeout=30)
        token = str(payload.get("csrf_token") or "").strip()
        if not token:
            raise ProbeError("server did not return csrf_token")
        self.csrf_token = token

    def login(self, username: str, password: str):
        self.fetch_csrf()
        payload = self.request_json(
            "/api/login",
            method="POST",
            payload={"username": username, "password": password},
            allow_http_error=True,
            timeout=60,
        )
        if payload.get("_http_status") != 200 or payload.get("ok") is not True:
            raise ProbeError(f"login failed: {payload.get('msg') or payload}")
        self.fetch_csrf()
        return payload


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += int(item.stat().st_size)
        except OSError:
            continue
    return total


def repo_cache_dir(cache_root: str, repo_id: str) -> Path | None:
    raw_root = str(cache_root or "").strip()
    raw_repo = str(repo_id or "").strip()
    if not raw_root or "/" not in raw_repo:
        return None
    return Path(raw_root).expanduser() / "hub" / ("models--" + raw_repo.replace("/", "--"))


def cache_status(cache_root: str, repo_id: str) -> dict:
    path = repo_cache_dir(cache_root, repo_id)
    if not path:
        return {"configured": False}
    return {
        "configured": True,
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": dir_size_bytes(path) if path.exists() else 0,
    }


def node_options(object_info: dict, node_class: str, input_name: str) -> set[str] | None:
    node = object_info.get(node_class) if isinstance(object_info, dict) else None
    required = ((node or {}).get("input") or {}).get("required") or {}
    raw = required.get(input_name)
    if isinstance(raw, list) and raw:
        first = raw[0]
        if isinstance(first, list):
            return {str(item) for item in first if str(item).strip()}
        if isinstance(first, str) and len(raw) > 1 and isinstance(raw[1], dict):
            options = raw[1].get("options")
            if isinstance(options, list):
                return {str(item) for item in options if str(item).strip()}
    return None


def write_bytes(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": str(path), "size_bytes": len(data)}


def save_data_url(path: Path, data_url: str) -> dict:
    if not data_url.startswith("data:") or "," not in data_url:
        return {"ok": False, "detail": "not a data URL"}
    header, encoded = data_url.split(",", 1)
    suffix = ".png"
    if "image/jpeg" in header or "image/jpg" in header:
        suffix = ".jpg"
    target = path.with_suffix(suffix)
    return {"ok": True, **write_bytes(target, base64.b64decode(encoded))}


def build_sdxl_workflow(*, model_name: str, prompt: str, negative_prompt: str, width: int, height: int, steps: int, cfg: float, seed: int):
    return {
        "1": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": model_name},
        },
        "2": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["1", 1]},
        },
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative_prompt, "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": int(width), "height": int(height), "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": int(seed),
                "steps": int(steps),
                "cfg": float(cfg),
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1,
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["1", 2]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": "hackme_channel_smoke/regular"},
        },
    }


def run_regular(args, out_dir: Path) -> dict:
    started = time.perf_counter()
    run = {"label": "regular_comfyui", "started_at": now_iso(), "model": args.regular_model}
    client = ComfyUIClient(args.comfyui_url, timeout=args.request_timeout)
    try:
        object_info = client.get_object_info()
        options = node_options(object_info, "CheckpointLoaderSimple", "ckpt_name")
        run["preflight"] = {
            "checkpoint_loader_available": "CheckpointLoaderSimple" in object_info,
            "model_available": bool(options is None or args.regular_model in options),
            "checkpoint_option_count": len(options or []),
        }
        if options is not None and args.regular_model not in options:
            run["ok"] = False
            run["error"] = "regular model not installed on remote ComfyUI"
            return run
        if args.preflight_only:
            run["ok"] = True
            run["skipped_generation"] = True
            return run
        workflow = build_sdxl_workflow(
            model_name=args.regular_model,
            prompt=args.prompt,
            negative_prompt=args.negative_prompt,
            width=args.width,
            height=args.height,
            steps=args.steps,
            cfg=args.cfg,
            seed=args.seed,
        )
        output = client.generate_from_workflow(
            workflow,
            timeout_seconds=args.max_seconds,
            expected_count=1,
            fetch_outputs=True,
        )
        data = output.get("data") or b""
        saved = write_bytes(out_dir / "regular_comfyui.png", data) if data else {}
        run["ok"] = bool(data)
        run["output"] = {
            "prompt_id": output.get("prompt_id"),
            "image_count": len(output.get("images") or []),
            "mime_type": output.get("mime_type"),
            **({"saved": saved} if saved else {}),
        }
        if not data:
            run["error"] = "ComfyUI completed without fetched image bytes"
    except Exception as exc:
        run["ok"] = False
        run["error"] = str(exc)
    finally:
        run["duration_seconds"] = round(time.perf_counter() - started, 3)
    return run


def configure_diffusers(client: WebClient, args, *, model_repo: str, token: str = "") -> dict:
    payload = {
        "comfyui_connection_mode": "diffusers",
        "comfyui_remote_api_url": args.comfyui_url,
        "comfyui_diffusers_model_repo": model_repo,
        "comfyui_huggingface_cache_root": args.hf_cache_root,
        "comfyui_allow_in_process_diffusers": True,
        "comfyui_diffusers_device": args.device,
        "comfyui_diffusers_dtype": args.dtype,
        "comfyui_diffusers_device_map": args.device_map,
        "comfyui_diffusers_low_cpu_mem_usage": args.low_cpu_mem_usage,
        "comfyui_diffusers_cuda_fallback_to_cpu": args.cuda_fallback_to_cpu,
        "comfyui_diffusers_keep_downloaded_models": args.keep_downloaded_models,
        "comfyui_diffusers_disable_xet": args.disable_xet,
    }
    if token:
        payload["comfyui_huggingface_api_token"] = token
    response = client.request_json(
        "/api/admin/settings",
        method="PUT",
        payload=payload,
        allow_http_error=True,
        timeout=60,
    )
    client.fetch_csrf()
    return {"ok": response.get("ok") is True and response.get("_http_status") == 200, "http_status": response.get("_http_status"), "msg": response.get("msg")}


def clear_huggingface_token(client: WebClient) -> dict:
    response = client.request_json(
        "/api/admin/settings",
        method="PUT",
        payload={"comfyui_huggingface_api_token_clear": True},
        allow_http_error=True,
        timeout=60,
    )
    client.fetch_csrf()
    return {"ok": response.get("ok") is True and response.get("_http_status") == 200, "http_status": response.get("_http_status"), "msg": response.get("msg")}


def inspect_diffusers(client: WebClient, model_repo: str) -> dict:
    payload = client.request_json(
        "/api/comfyui/diffusers/inspect",
        method="POST",
        payload={"diffusers_model_repo": model_repo, "generation_mode": "txt2img"},
        allow_http_error=True,
        timeout=120,
    )
    compact = {key: payload.get(key) for key in (
        "ok",
        "msg",
        "model_repo",
        "repo_id",
        "model_kind",
        "suggested_base_repo",
        "variant_options",
        "gguf_files",
        "_http_status",
    ) if key in payload}
    return compact


def preview_first_image(client: WebClient, result: dict, out_path: Path) -> dict:
    result = result if isinstance(result, dict) else {}
    images = result.get("images") if isinstance(result.get("images"), list) else []
    image = images[0] if images else result.get("image")
    image_ref = image.get("image_ref") if isinstance(image, dict) else None
    if not isinstance(image_ref, dict):
        return {"ok": False, "detail": "no image_ref"}
    payload = client.request_json(
        "/api/comfyui/image-preview",
        method="POST",
        payload={"image_ref": image_ref},
        allow_http_error=True,
        timeout=60,
    )
    image_payload = payload.get("image") if isinstance(payload.get("image"), dict) else {}
    data_url = str(image_payload.get("data_url") or "")
    saved = save_data_url(out_path, data_url) if data_url else {"ok": False, "detail": "no data_url"}
    return {
        "ok": payload.get("_http_status") == 200 and payload.get("ok") is True and saved.get("ok") is True,
        "http_status": payload.get("_http_status"),
        "mime_type": image_payload.get("mime_type"),
        "size_bytes": image_payload.get("size_bytes"),
        "saved": saved if saved.get("ok") else None,
    }


def run_app_generation(client: WebClient, args, *, label: str, payload: dict, out_path: Path) -> dict:
    started = time.perf_counter()
    run = {"label": label, "started_at": now_iso(), "payload": {key: value for key, value in payload.items() if key != "negative_prompt"}}
    start = client.request_json(
        "/api/comfyui/generate",
        method="POST",
        payload=payload,
        allow_http_error=True,
        timeout=120,
    )
    job_id = ((start.get("job") or {}) if isinstance(start.get("job"), dict) else {}).get("job_id")
    run["start_response"] = {"ok": start.get("ok"), "http_status": start.get("_http_status"), "msg": start.get("msg"), "job_id": job_id}
    if start.get("_http_status") != 200 or start.get("ok") is not True or not job_id:
        run["ok"] = False
        run["error"] = start.get("msg") or "job did not start"
        run["duration_seconds"] = round(time.perf_counter() - started, 3)
        return run

    last_status = ""
    progress_tail = []
    while time.perf_counter() - started <= args.max_seconds:
        job_payload = client.request_json(
            f"/api/comfyui/jobs/{urllib.parse.quote(str(job_id))}",
            allow_http_error=True,
            timeout=60,
        )
        job = job_payload.get("job") if isinstance(job_payload.get("job"), dict) else {}
        last_status = str(job.get("status") or "").strip().lower()
        progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
        progress_tail.append({key: progress.get(key) for key in ("phase", "percent", "step", "detail", "current_file", "bytes_written", "total_bytes", "cache_hit") if key in progress})
        progress_tail = progress_tail[-8:]
        if last_status in {"completed", "failed", "error", "cancelled"}:
            run["final_job"] = {
                "ok": job_payload.get("ok"),
                "http_status": job_payload.get("_http_status"),
                "status": job.get("status"),
                "error": job.get("error"),
                "progress_tail": progress_tail,
            }
            result = job.get("result") if isinstance(job.get("result"), dict) else {}
            images = result.get("images") if isinstance(result.get("images"), list) else []
            run["output"] = {"image_count": len(images), "media_count": len(result.get("media") or []) if isinstance(result.get("media"), list) else 0}
            run["preview"] = preview_first_image(client, result, out_path) if images else {"ok": False, "detail": "no image output"}
            run["ok"] = last_status == "completed" and bool(images) and run["preview"].get("ok") is True
            if not run["ok"]:
                run["error"] = job.get("error") or job_payload.get("msg") or "job did not produce a previewable image"
            break
        time.sleep(max(0.5, float(args.poll_seconds)))
    if "final_job" not in run:
        run["ok"] = False
        run["error"] = f"timeout waiting for job; last_status={last_status}"
        run["progress_tail"] = progress_tail
    run["duration_seconds"] = round(time.perf_counter() - started, 3)
    return run


def first_gguf_file(inspection: dict) -> str:
    for key in ("gguf_files", "variant_options"):
        values = inspection.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if isinstance(item, dict):
                candidate = str(item.get("path") or item.get("filename") or item.get("value") or "").strip()
            else:
                candidate = str(item or "").strip()
            if candidate.startswith("gguf::"):
                candidate = candidate.split("::", 1)[1].strip()
            if candidate.lower().endswith(".gguf"):
                return candidate
    return ""


def run_diffusers_channel(client: WebClient, args, out_dir: Path, *, token: str) -> dict:
    started = time.perf_counter()
    run = {"label": "hf_diffusers", "started_at": now_iso(), "model": args.hf_model, "cache": cache_status(args.hf_cache_root, args.hf_model)}
    try:
        run["settings"] = configure_diffusers(client, args, model_repo=args.hf_model, token=token)
        if not run["settings"].get("ok"):
            raise ProbeError(f"failed to configure Diffusers settings: {run['settings'].get('msg')}")
        run["inspection"] = inspect_diffusers(client, args.hf_model)
        if args.preflight_only:
            run["ok"] = bool(run["inspection"].get("ok"))
            run["skipped_generation"] = True
            return run
        payload = {
            "generation_mode": "txt2img",
            "prompt": args.prompt,
            "negative_prompt": args.negative_prompt,
            "width": args.width,
            "height": args.height,
            "steps": args.steps,
            "cfg": args.cfg,
            "seed": args.seed + 1,
            "batch_size": 1,
            "confirm_billing": True,
            "async_progress": True,
            "timeout_seconds": 0,
            "diffusers_model_repo": args.hf_model,
            "diffusers_model_variant": args.diffusers_variant,
        }
        run.update(run_app_generation(client, args, label="hf_diffusers", payload=payload, out_path=out_dir / "hf_diffusers"))
    except Exception as exc:
        run["ok"] = False
        run["error"] = str(exc)
    finally:
        run["cache_after"] = cache_status(args.hf_cache_root, args.hf_model)
        run["duration_seconds"] = round(time.perf_counter() - started, 3)
    return run


def run_gguf_channel(client: WebClient, args, out_dir: Path, *, token: str) -> dict:
    started = time.perf_counter()
    run = {"label": "gguf", "started_at": now_iso(), "model": args.gguf_model, "cache": cache_status(args.hf_cache_root, args.gguf_model)}
    try:
        run["settings"] = configure_diffusers(client, args, model_repo=args.gguf_model, token=token)
        if not run["settings"].get("ok"):
            raise ProbeError(f"failed to configure GGUF settings: {run['settings'].get('msg')}")
        run["inspection"] = inspect_diffusers(client, args.gguf_model)
        gguf_file = args.gguf_file or first_gguf_file(run["inspection"])
        run["selected_gguf_file"] = gguf_file
        if not gguf_file:
            run["ok"] = False
            run["error"] = "no GGUF file found by inspect; provide --gguf-file"
            return run
        if args.preflight_only:
            run["ok"] = bool(run["inspection"].get("ok"))
            run["skipped_generation"] = True
            return run
        base_repo = args.gguf_base_repo or str(run["inspection"].get("suggested_base_repo") or "stabilityai/stable-diffusion-xl-base-1.0")
        payload = {
            "generation_mode": "txt2img",
            "prompt": args.prompt,
            "negative_prompt": args.negative_prompt,
            "width": args.width,
            "height": args.height,
            "steps": args.steps,
            "cfg": args.cfg,
            "seed": args.seed + 2,
            "batch_size": 1,
            "confirm_billing": True,
            "async_progress": True,
            "timeout_seconds": 0,
            "diffusers_model_repo": args.gguf_model,
            "diffusers_model_variant": f"gguf::{gguf_file}",
            "diffusers_gguf_file": gguf_file,
            "diffusers_gguf_base_repo": base_repo,
        }
        run.update(run_app_generation(client, args, label="gguf", payload=payload, out_path=out_dir / "gguf"))
    except Exception as exc:
        run["ok"] = False
        run["error"] = str(exc)
    finally:
        run["cache_after"] = cache_status(args.hf_cache_root, args.gguf_model)
        run["duration_seconds"] = round(time.perf_counter() - started, 3)
    return run


def read_token(args) -> str:
    token = ""
    if args.hf_token_env:
        token = str(os.environ.get(args.hf_token_env, "") or "").strip()
    if not token and args.hf_token_file:
        token = Path(args.hf_token_file).expanduser().read_text(encoding="utf-8").strip()
    if not token and args.hf_token_stdin:
        token = sys.stdin.readline().strip()
    return token


def parse_bool_text(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def parse_args():
    parser = argparse.ArgumentParser(description="Run isolated ComfyUI regular/HF/GGUF channel smoke checks.")
    parser.add_argument("--base-url", default="", help="hackme_web base URL; needed for diffusers/gguf channels.")
    parser.add_argument("--username", default="root")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser, "--password")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--comfyui-url", default="http://192.168.18.19:8188")
    parser.add_argument("--hf-cache-root", default=os.environ.get("HF_PROBE_CACHE_ROOT", ""))
    parser.add_argument("--out-dir", default="/tmp/hackme_comfyui_channel_smoke")
    parser.add_argument("--out-json", default="")
    parser.add_argument("--run", action="append", choices=["regular", "diffusers", "gguf"], help="Run subset; defaults to all.")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--negative-prompt", default=DEFAULT_NEGATIVE)
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--cfg", type=float, default=5.0)
    parser.add_argument("--seed", type=int, default=20260528)
    parser.add_argument("--max-seconds", type=int, default=1200)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--request-timeout", type=int, default=45)
    parser.add_argument("--regular-model", default=DEFAULT_REGULAR_MODEL)
    parser.add_argument("--hf-model", default=DEFAULT_HF_MODEL)
    parser.add_argument("--diffusers-variant", default="fp16")
    parser.add_argument("--gguf-model", default=DEFAULT_GGUF_MODEL)
    parser.add_argument("--gguf-file", default="")
    parser.add_argument("--gguf-base-repo", default="")
    parser.add_argument("--hf-token-env", default="HF_TOKEN")
    parser.add_argument("--hf-token-file", default="")
    parser.add_argument("--hf-token-stdin", action="store_true", help="Read the HF token from stdin instead of argv/env/file.")
    parser.add_argument("--keep-hf-token", action="store_true", help="Leave supplied HF token in root settings after the smoke run.")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--dtype", default="auto")
    parser.add_argument("--device-map", default="auto")
    parser.add_argument("--low-cpu-mem-usage", type=parse_bool_text, default=True)
    parser.add_argument("--cuda-fallback-to-cpu", type=parse_bool_text, default=True)
    parser.add_argument("--keep-downloaded-models", type=parse_bool_text, default=True)
    parser.add_argument("--disable-xet", type=parse_bool_text, default=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    selected = set(args.run or ["regular", "diffusers", "gguf"])
    token = read_token(args)
    report = {
        "ok": False,
        "started_at": now_iso(),
        "selected": sorted(selected),
        "comfyui_url": args.comfyui_url,
        "base_url": args.base_url,
        "hf_cache_root": args.hf_cache_root,
        "hf_token_supplied": bool(token),
        "dimensions": {"width": args.width, "height": args.height, "steps": args.steps},
        "artifacts_dir": str(out_dir),
        "runs": [],
    }
    web_client = None
    try:
        if "regular" in selected:
            report["runs"].append(run_regular(args, out_dir))
        if selected.intersection({"diffusers", "gguf"}):
            if not args.base_url:
                raise ProbeError("--base-url is required for diffusers/gguf checks")
            web_client = WebClient(args.base_url, insecure=args.insecure)
            web_client.login(args.username, args.password)
        if "diffusers" in selected:
            report["runs"].append(run_diffusers_channel(web_client, args, out_dir, token=token))
        if "gguf" in selected:
            report["runs"].append(run_gguf_channel(web_client, args, out_dir, token=token))
        if web_client and token and not args.keep_hf_token:
            try:
                report["hf_token_clear"] = clear_huggingface_token(web_client)
            except Exception as exc:
                report["hf_token_clear"] = {"ok": False, "error": str(exc)}
        report["ok"] = all(run.get("ok") is True for run in report["runs"])
    except Exception as exc:
        report["ok"] = False
        report["error"] = str(exc)
    report["finished_at"] = now_iso()
    out_json = Path(args.out_json or (out_dir / "isolated_channel_smoke.json")).expanduser().resolve()
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps({
        "ok": report.get("ok"),
        "out_json": str(out_json),
        "runs": [
            {
                "label": run.get("label"),
                "ok": run.get("ok"),
                "duration_seconds": run.get("duration_seconds"),
                "error": run.get("error"),
                "output": run.get("output"),
                "preview": run.get("preview"),
            }
            for run in report.get("runs", [])
        ],
    }, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
