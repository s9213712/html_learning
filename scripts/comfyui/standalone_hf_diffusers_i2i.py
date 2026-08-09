#!/usr/bin/env python3
"""Offline-only Diffusers img2img validation helper.

This stays outside the web process so an operator can verify a cached
Diffusers model without switching the product runtime away from ComfyUI.
It deliberately refuses to download a model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
import types
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MODEL = "dhead/wai-nsfw-illustrious-sdxl-v140-sdxl"
DEFAULT_PROMPT = (
    "a fully clothed illustrated character; preserve blonde hair, white shirt, "
    "blue shorts, full-body standing pose, centered placement, and clean image details"
)
DEFAULT_NEGATIVE = "nude, naked, explicit, blurry, watermark, text, distorted anatomy"


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _diffusers_client_class():
    """Import the product client, with a Windows-only harness compatibility shim.

    The deployed web process is Linux and uses the normal ``client`` module.
    This offline visual runner can also reuse ComfyUI's Windows embedded CUDA
    Python, whose interpreter has no ``fcntl``.  ``DiffusersClient`` only
    needs these two value types from that module, so providing them here avoids
    importing Linux-only workflow-cleanup code without changing production
    behavior or the Diffusers execution implementation under test.
    """
    if os.name == "nt" and "services.comfyui.client" not in sys.modules:
        shim = types.ModuleType("services.comfyui.client")

        class ComfyUIError(RuntimeError):
            pass

        @dataclass
        class ComfyUIImage:
            filename: str
            subfolder: str
            type: str
            mime_type: str
            data: bytes

        shim.ComfyUIError = ComfyUIError
        shim.ComfyUIImage = ComfyUIImage
        sys.modules["services.comfyui.client"] = shim
    from services.comfyui.diffusers_client import DiffusersClient

    return DiffusersClient


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run a cached Diffusers image-to-image validation with no network downloads.")
    parser.add_argument("--source-image-path", required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--negative-prompt", default=DEFAULT_NEGATIVE)
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=640)
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--strength", type=float, default=0.25)
    parser.add_argument("--guidance-scale", type=float, default=5.0)
    parser.add_argument("--seed", type=int, default=123456789)
    parser.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    parser.add_argument("--dtype", choices=("auto", "float16", "bfloat16", "float32"), default="auto")
    parser.add_argument("--device-map", choices=("auto", "cuda", "balanced", "balanced_low_0", "sequential", "disabled"), default="auto")
    parser.add_argument("--huggingface-cache-root", default="", help="Existing Hugging Face cache root; no model download is attempted.")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--offline", action=argparse.BooleanOptionalAction, default=True, help="Force HF/Transformers offline mode; enabled by default for this validation helper.")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    source_path = Path(args.source_image_path).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "ok": False,
        "network": "disabled via local_files_only=True",
        "model": args.model,
        "source": str(source_path),
        "output": str(output_path),
        "settings": {
            "width": args.width,
            "height": args.height,
            "steps": args.steps,
            "strength": args.strength,
            "guidance_scale": args.guidance_scale,
            "seed": args.seed,
            "device": args.device,
            "dtype": args.dtype,
            "device_map": args.device_map,
            "offline": bool(args.offline),
        },
    }
    started = time.perf_counter()
    try:
        if not source_path.is_file():
            raise ValueError(f"source image does not exist: {source_path}")
        if args.width <= 0 or args.height <= 0 or args.width % 8 or args.height % 8:
            raise ValueError("width and height must be positive multiples of 8")
        if args.steps < 2 or not 0 < args.strength <= 1:
            raise ValueError("steps must be at least 2 and strength must be in (0, 1]")

        if args.offline:
            # The validation must prove the cached model is usable.  If any
            # component is absent, fail instead of quietly downloading it.
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
        from PIL import Image
        DiffusersClient = _diffusers_client_class()

        with Image.open(source_path) as source:
            source.verify()

        progress_events = []

        def progress_callback(payload):
            if not isinstance(payload, dict):
                return
            progress_events.append({
                key: payload.get(key)
                for key in ("phase", "percent", "step", "detail", "backend_kind", "cache_hit")
                if payload.get(key) not in (None, "")
            })

        client = DiffusersClient(
            model_repo=args.model,
            storage_root=output_path.parent,
            device=args.device,
            dtype=args.dtype,
            device_map=args.device_map,
            allow_in_process_runtime=True,
            cuda_fallback_to_cpu=False,
            huggingface_cache_root=args.huggingface_cache_root,
        )
        source_ref = client.upload_image_bytes(source_path.read_bytes(), source_path.name, image_type="input")
        result = client.generate_image(
            {
                "generation_mode": "img2img",
                "model": args.model,
                "diffusers_model_repo": args.model,
                # Mirror the UI's explicit default-weight selection.  Without
                # this, the client correctly queries HF metadata to disambiguate
                # multi-variant repos, which would defeat an offline cache test.
                "diffusers_model_variant": "__default__",
                "diffusers_model_variant_selected": True,
                "source_image_ref": source_ref,
                "prompt": args.prompt,
                "negative_prompt": args.negative_prompt,
                "width": args.width,
                "height": args.height,
                "steps": args.steps,
                "cfg": args.guidance_scale,
                "denoise_strength": args.strength,
                "seed": args.seed,
                "batch_size": 1,
            },
            timeout_seconds=args.timeout_seconds,
            progress_callback=progress_callback,
        )
        image_data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(image_data, (bytes, bytearray)) or not image_data:
            raise RuntimeError("Diffusers client returned no image bytes")
        output_path.write_bytes(bytes(image_data))
        report.update({
            "ok": True,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "output_bytes": output_path.stat().st_size,
            "backend_kind": client.backend_kind,
            "image_ref": result.get("image_ref") if isinstance(result, dict) else None,
            "progress_tail": progress_events[-12:],
        })
    except Exception as exc:
        report.update({
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "error": f"{exc.__class__.__name__}: {exc}",
            "traceback": traceback.format_exc(limit=10),
        })
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
