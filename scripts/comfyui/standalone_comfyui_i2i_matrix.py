#!/usr/bin/env python3
"""Standalone ComfyUI-only img2img semantic matrix probe.

The script talks directly to a ComfyUI HTTP API.  It does not import
hackme_web, so it can be copied to a remote ComfyUI machine for live I2I
reprobes.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path


DEFAULT_MODEL = "SDXL\\illustrious(IL)\\WAI系列\\waiIllustriousSDXL_v160.safetensors"
MODEL_FALLBACK_KEYWORDS = (
    "waiillustrioussdxl_v160",
    "jankutrainedchenkinnoobai_v777",
    "animagine-xl-4.0",
    "illustrious",
    "sdxl",
)
DEFAULT_SOURCE_PROMPT = (
    "anime style, adult woman, solo, cat girl, bikini, laying on the beach, "
    "red beach ball near her right side, blue beach umbrella in the background, "
    "ocean, sunny sky, clean lineart, detailed"
)
LEGACY_2GIRLS_PROMPT = (
    "adult women, fully clothed, by ogipote, 2girls, girls love, kiss, "
    "saliva, maid uniform, cat ears, cat tail"
)
DEFAULT_NEGATIVE_PROMPT = (
    "child, minor, underage, loli, teen, explicit, nude, naked, monochrome, "
    "text, watermark, low quality, blurry, bad hand, bad fingers, bad legs, "
    "bad anatomy, deformed"
)
SENSITIVE_RE = re.compile(r"hf_[A-Za-z0-9]{8,}|(Bearer\s+)[A-Za-z0-9._-]+", re.IGNORECASE)


CONTROLNET_TYPES = {
    "canny": {
        "preprocessors": ("CannyEdgePreprocessor",),
        "keywords": ("canny",),
    },
    "openpose": {
        "preprocessors": ("OpenposePreprocessor", "DWPreprocessor"),
        "keywords": ("openpose", "pose"),
    },
    "depth": {
        "preprocessors": ("DepthAnythingPreprocessor", "MiDaS-DepthMapPreprocessor"),
        "keywords": ("depth",),
    },
    "lineart": {
        "preprocessors": ("LineArtPreprocessor", "LineartStandardPreprocessor"),
        "keywords": ("lineart", "line-art"),
    },
}


class ProbeError(RuntimeError):
    pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sanitize_text(value) -> str:
    text = str(value or "")
    return SENSITIVE_RE.sub(lambda match: (match.group(1) or "") + "***" if match.group(1) else "hf_***", text)


def _ask_text(label: str, current):
    shown = str(current or "")
    value = input(f"{label} [{shown}]: ").strip()
    return value or current


def _ask_choice(label: str, current: str, choices: tuple[str, ...]) -> str:
    allowed = ", ".join(choices)
    while True:
        value = input(f"{label} ({allowed}) [{current}]: ").strip()
        if not value:
            return current
        if value in choices:
            return value
        print(f"Enter one of: {allowed}", file=sys.stderr)


def _ask_int(label: str, current: int) -> int:
    while True:
        value = input(f"{label} [{current}]: ").strip()
        if not value:
            return int(current)
        try:
            return int(value)
        except ValueError:
            print("Enter an integer.", file=sys.stderr)


def _ask_float(label: str, current: float) -> float:
    while True:
        value = input(f"{label} [{current}]: ").strip()
        if not value:
            return float(current)
        try:
            return float(value)
        except ValueError:
            print("Enter a number.", file=sys.stderr)


def _case_options() -> str:
    return (
        "blank for all, img2img_redraw_sunset, img2img_style_watercolor, "
        "img2img_feature_preserve, inpaint_remove_repair, inpaint_replace_edit, "
        "outpaint_expand_beach, controlnet_copy_composition_canny/openpose/depth/lineart, "
        "upscale_redraw_imagescale, two_image_blend_mix, ipadapter_style_reference, "
        "ipadapter_inpaint_reference"
    )


def apply_interactive_prompts(args):
    if not getattr(args, "interactive", False):
        return args
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ProbeError("--interactive requires a TTY; omit it for non-interactive CLI runs.")
    print("Interactive ComfyUI I2I matrix probe. Press Enter to keep the shown value.")
    args.comfyui_url = _ask_text("ComfyUI URL", args.comfyui_url)
    args.model = _ask_text("Checkpoint model name", args.model)
    args.prompt_suite = _ask_choice("Prompt suite", args.prompt_suite, ("beach_catgirl", "legacy_2girls"))
    args.source_image_path = _ask_text("Source image path (blank = generate source)", args.source_image_path)
    print(f"Only-case options: {_case_options()}")
    args.only_case = _ask_text("Only case", args.only_case)
    args.case_prompt = _ask_text("Case prompt override", args.case_prompt)
    args.case_denoise = _ask_float("Case denoise override (0 = case default)", args.case_denoise)
    args.prompt = _ask_text("Base positive prompt", args.prompt)
    args.source_prompt = _ask_text("Source positive prompt", args.source_prompt)
    args.negative_prompt = _ask_text("Negative prompt", args.negative_prompt)
    args.width = _ask_int("Width", args.width)
    args.height = _ask_int("Height", args.height)
    args.steps = _ask_int("Steps", args.steps)
    args.cfg = _ask_float("CFG", args.cfg)
    args.seed = _ask_int("Seed", args.seed)
    args.controlnet_type = _ask_text("ControlNet type", args.controlnet_type)
    args.controlnet_model = _ask_text("ControlNet model override", args.controlnet_model)
    args.control_strength = _ask_float("ControlNet strength", args.control_strength)
    args.inpaint_method = _ask_choice("Inpaint method", args.inpaint_method, ("auto", "conditioning", "vae_encode"))
    args.outpaint_method = _ask_choice(
        "Outpaint method",
        args.outpaint_method,
        ("auto", "full_redraw", "conditioning", "vae_encode"),
    )
    args.mask_shape = _ask_choice("Mask shape", args.mask_shape, ("default", "window", "background_wall", "small_wall", "kimono_clothes"))
    args.outpaint = _ask_int("Outpaint default pixels", args.outpaint)
    args.outpaint_source_feather = _ask_int("Outpaint source preservation feather pixels", args.outpaint_source_feather)
    args.outpaint_seam_prefill = _ask_choice(
        "Outpaint seam prefill",
        args.outpaint_seam_prefill,
        ("auto", "on", "off"),
    )
    args.outpaint_preserve_source = _ask_choice(
        "Preserve source interior after outpaint",
        "yes" if args.outpaint_preserve_source else "no",
        ("yes", "no"),
    ) == "yes"
    args.blend_image_path = _ask_text("Blend image path", args.blend_image_path)
    args.style_image_path = _ask_text("Style/reference image path", args.style_image_path)
    args.out_dir = _ask_text("Output directory", args.out_dir)
    return args


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip()).strip("_") or "case"


def windows_equivalent_path(value: str) -> str:
    raw = str(value or "").strip()
    if os.name == "nt" and raw.startswith("/mnt/") and len(raw) >= 6 and raw[5].isalpha():
        drive = raw[5].upper()
        rest = raw[6:].lstrip("/").replace("/", "\\")
        return f"{drive}:\\{rest}" if rest else f"{drive}:\\"
    return raw


def normalize_runtime_paths(args):
    if os.name != "nt":
        return args
    for name in ("out_dir", "out_json", "source_image_path", "blend_image_path", "style_image_path"):
        if hasattr(args, name):
            setattr(args, name, windows_equivalent_path(getattr(args, name)))
    return args


class ComfyClient:
    def __init__(self, base_url: str, *, insecure: bool = False, timeout: int = 60):
        self.base_url = str(base_url or "").rstrip("/")
        self.timeout = int(timeout or 60)
        handlers = []
        if self.base_url.startswith("https://"):
            context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
            handlers.append(urllib.request.HTTPSHandler(context=context))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [("User-Agent", "hackme-comfyui-i2i-matrix/1.0")]

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    @staticmethod
    def _read_body(resp) -> bytes:
        try:
            return resp.read()
        except http.client.IncompleteRead as exc:
            return exc.partial or b""

    def json(self, path: str, *, method="GET", payload=None, timeout=None) -> dict:
        body = None
        headers = {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self._url(path), data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = self._read_body(resp)
        except urllib.error.HTTPError as exc:
            raw = self._read_body(exc)
            raise ProbeError(f"{method} {path} HTTP {exc.code}: {raw[:800]!r}") from exc
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {"raw": data}

    def bytes(self, path: str, *, timeout=None) -> bytes:
        req = urllib.request.Request(self._url(path), method="GET")
        with self.opener.open(req, timeout=timeout or self.timeout) as resp:
            return self._read_body(resp)

    def multipart(self, path: str, *, fields=None, files=None, timeout=None) -> dict:
        boundary = f"----HackmeComfyI2I{uuid.uuid4().hex}"
        body = bytearray()
        for key, value in (fields or {}).items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")
        for item in files or []:
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                f'Content-Disposition: form-data; name="{item["field"]}"; filename="{item["filename"]}"\r\n'.encode("utf-8")
            )
            body.extend(f'Content-Type: {item.get("content_type") or "application/octet-stream"}\r\n\r\n'.encode("utf-8"))
            body.extend(item.get("data") or b"")
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        req = urllib.request.Request(
            self._url(path),
            data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self.opener.open(req, timeout=timeout or self.timeout) as resp:
                raw = self._read_body(resp)
        except urllib.error.HTTPError as exc:
            raw = self._read_body(exc)
            raise ProbeError(f"POST {path} HTTP {exc.code}: {raw[:800]!r}") from exc
        data = json.loads(raw.decode("utf-8", errors="replace"))
        return data if isinstance(data, dict) else {"raw": data}

    def upload_image(self, path: Path, *, overwrite=False) -> dict:
        payload = self.multipart(
            "/upload/image",
            fields={"type": "input", "overwrite": "true" if overwrite else "false", "subfolder": ""},
            files=[{
                "field": "image",
                "filename": path.name,
                "content_type": "image/png",
                "data": path.read_bytes(),
            }],
            timeout=max(self.timeout, 120),
        )
        name = str(payload.get("name") or path.name).strip()
        if not name:
            raise ProbeError(f"ComfyUI upload did not return a filename for {path}")
        return {
            "filename": name,
            "subfolder": str(payload.get("subfolder") or "").strip(),
            "type": str(payload.get("type") or "input").strip() or "input",
        }


def object_input_meta(object_info: dict, node_class: str, input_name: str):
    node = object_info.get(node_class) if isinstance(object_info, dict) else None
    inputs = (node or {}).get("input") or {}
    required = inputs.get("required") or {}
    optional = inputs.get("optional") or {}
    if input_name in required:
        return required.get(input_name)
    return optional.get(input_name)


def node_options(object_info: dict, node_class: str, input_name: str) -> list[str]:
    raw = object_input_meta(object_info, node_class, input_name)
    if isinstance(raw, list):
        if raw and isinstance(raw[0], list):
            return [str(item) for item in raw[0] if str(item).strip()]
        if len(raw) > 1 and isinstance(raw[1], dict):
            options = raw[1].get("options") or raw[1].get("values")
            if isinstance(options, list):
                return [str(item) for item in options if str(item).strip()]
    return []


def input_default(object_info: dict, node_class: str, input_name: str):
    raw = object_input_meta(object_info, node_class, input_name)
    if isinstance(raw, list) and len(raw) > 1 and isinstance(raw[1], dict):
        if "default" in raw[1]:
            return raw[1]["default"]
    options = node_options(object_info, node_class, input_name)
    return options[0] if options else None


def required_default_inputs(object_info: dict, node_class: str, provided: dict) -> dict:
    node = object_info.get(node_class) if isinstance(object_info, dict) else None
    node_inputs = (node or {}).get("input") or {}
    expected = {}
    expected.update(node_inputs.get("required") or {})
    expected.update(node_inputs.get("optional") or {})
    inputs = dict(provided)
    for key in expected:
        if key in inputs:
            continue
        default = input_default(object_info, node_class, key)
        if default is not None:
            inputs[key] = default
    return inputs


def has_node(object_info: dict, node_class: str) -> bool:
    return isinstance(object_info, dict) and node_class in object_info


def is_sdxl_checkpoint_name(model_name: str) -> bool:
    """Return true only when the selected checkpoint explicitly identifies as SDXL.

    IPAdapterStyleComposition is an SDXL-only node.  CheckpointLoaderSimple
    exposes a filename rather than a model-family capability, so this is kept
    deliberately conservative: an unknown filename is skipped instead of
    submitting a workflow which ComfyUI will deterministically reject.
    """

    normalized = re.sub(r"[^a-z0-9]+", "", str(model_name or "").lower())
    return "sdxl" in normalized


def choose_sdxl_ipadapter_assets(object_info: dict) -> dict | None:
    """Find a matching explicit SDXL IPAdapter and CLIP-Vision pair.

    The Unified Loader maps presets to a hard-coded filename convention.  A
    valid local installation may use a different convention (for example,
    ``ip-adapter-plus_sdxl_vit-h.safetensors``), so use the instance's actual
    option lists instead.  This keeps the matrix portable and avoids a false
    positive capability check followed by a queue-time model-not-found error.
    """

    required_nodes = ("IPAdapterModelLoader", "CLIPVisionLoader", "IPAdapterStyleComposition")
    if not all(has_node(object_info, node) for node in required_nodes):
        return None
    adapter_options = node_options(object_info, "IPAdapterModelLoader", "ipadapter_file")
    clip_options = node_options(object_info, "CLIPVisionLoader", "clip_name")
    sdxl_adapters = [name for name in adapter_options if "sdxl" in name.lower()]
    vit_h_clips = [name for name in clip_options if "vit-h" in name.lower() or "vision_h" in name.lower()]
    if not sdxl_adapters or not vit_h_clips:
        return None
    # Prefer the higher-fidelity plus model while retaining a valid standard
    # SDXL adapter as a fallback.
    adapter = next((name for name in sdxl_adapters if "plus" in name.lower()), sdxl_adapters[0])
    return {"ipadapter_file": adapter, "clip_name": vit_h_clips[0]}


def require_sdxl_ipadapter_assets(object_info: dict) -> dict:
    assets = choose_sdxl_ipadapter_assets(object_info)
    if not assets:
        raise ProbeError(
            "An explicit SDXL IPAdapter model, matching ViT-H CLIP Vision model, "
            "and IPAdapterStyleComposition node are required for this workflow."
        )
    return assets


def selected_inpaint_method(args, object_info: dict) -> str:
    method = str(getattr(args, "inpaint_method", "auto") or "auto").strip().lower()
    if method == "auto":
        return "conditioning" if has_node(object_info, "InpaintModelConditioning") else "vae_encode"
    return method


def selected_outpaint_method(args, object_info: dict) -> str:
    """Choose a sampling path that can actually replace padded pixels.

    Generic checkpoints frequently leave the neutral-gray ImagePadForOutpaint
    canvas untouched when sampling is constrained to an inpaint noise mask.
    For those checkpoints, redraw the padded image and composite the protected
    source interior back with a feathered mask.  Explicit method choices keep
    the masked paths available for dedicated inpainting checkpoints.
    """

    method = str(getattr(args, "outpaint_method", "auto") or "auto").strip().lower()
    if method != "auto":
        return method

    # Preserve the legacy explicit --inpaint-method behaviour for callers that
    # have not yet opted into --outpaint-method.
    legacy_method = str(getattr(args, "inpaint_method", "auto") or "auto").strip().lower()
    if legacy_method != "auto":
        return legacy_method
    return "full_redraw"


def outpaint_seam_prefill_enabled(args, object_info: dict) -> bool:
    """Use a pixel-space inpaint pass to remove gray padding before diffusion.

    ComfyUI's built-in outpaint padding starts at neutral gray.  A generic
    diffusion checkpoint can leave that gray visible, especially when the
    source is composited back for identity preservation.  MAT supplies a
    deterministic context fill first, giving the latent sampler image content
    rather than a hard gray rectangle at the transition.
    """

    # MAT prefill is useful only on models where it has been visually
    # validated.  Keeping it opt-in prevents an installed extension from
    # silently changing the established outpaint result.
    requested = str(getattr(args, "outpaint_seam_prefill", "off") or "off").strip().lower()
    available = all(
        has_node(object_info, name)
        for name in ("INPAINT_LoadInpaintModel", "INPAINT_InpaintWithModel")
    )
    if requested == "on" and not available:
        raise ProbeError("outpaint seam prefill requires INPAINT_LoadInpaintModel and INPAINT_InpaintWithModel")
    return requested != "off" and available


def outpaint_padding(args) -> dict[str, int]:
    default_expand = int(args.outpaint)
    return {
        "left": default_expand if args.outpaint_left is None else int(args.outpaint_left),
        "top": default_expand if args.outpaint_top is None else int(args.outpaint_top),
        "right": default_expand if args.outpaint_right is None else int(args.outpaint_right),
        "bottom": default_expand if args.outpaint_bottom is None else int(args.outpaint_bottom),
    }


def scaled_dimensions(width: int, height: int, factor: float, *, multiple: int = 8) -> tuple[int, int]:
    if int(width) <= 0 or int(height) <= 0:
        raise ProbeError("source dimensions must be positive")
    if float(factor) <= 1.0:
        raise ProbeError("--upscale-factor must be greater than 1.0")
    return tuple(
        max(multiple, int(round((int(value) * float(factor)) / multiple)) * multiple)
        for value in (width, height)
    )


def maybe_apply_differential_diffusion(args, object_info: dict, workflow: dict, model_ref: list) -> list:
    if not bool(getattr(args, "differential_diffusion", False)):
        return model_ref
    if not has_node(object_info, "DifferentialDiffusion"):
        return model_ref
    node_id = str(max(int(item) for item in workflow) + 1)
    workflow[node_id] = {
        "class_type": "DifferentialDiffusion",
        "inputs": required_default_inputs(
            object_info,
            "DifferentialDiffusion",
            {"model": model_ref, "strength": float(getattr(args, "differential_strength", 1.0))},
        ),
    }
    return [node_id, 0]


def resolve_choice(requested: str, options: list[str], *, label: str, allow_fallback=False) -> str:
    requested = str(requested or "").strip()
    if not options:
        if requested:
            return requested
        raise ProbeError(f"{label} options are unavailable")
    if requested in options:
        return requested
    requested_name = Path(requested.replace("\\", "/")).name.lower()
    for item in options:
        if Path(item.replace("\\", "/")).name.lower() == requested_name:
            return item
    if allow_fallback:
        lowered = [(item, item.lower().replace("\\", "/")) for item in options]
        for keyword in MODEL_FALLBACK_KEYWORDS:
            for item, low in lowered:
                if keyword in low:
                    return item
        return options[0]
    preview = ", ".join(options[:12])
    raise ProbeError(f"{label} is not available: {requested}. Available examples: {preview}")


def choose_sampler_settings(object_info: dict, sampler: str, scheduler: str) -> tuple[str, str]:
    sampler_options = node_options(object_info, "KSampler", "sampler_name")
    scheduler_options = node_options(object_info, "KSampler", "scheduler")
    sampler_name = sampler if not sampler_options or sampler in sampler_options else sampler_options[0]
    scheduler_name = scheduler if not scheduler_options or scheduler in scheduler_options else scheduler_options[0]
    return sampler_name, scheduler_name


def choose_controlnet(object_info: dict, requested_type: str, requested_model: str = "") -> dict | None:
    if "ControlNetLoader" not in object_info or "ControlNetApplyAdvanced" not in object_info:
        return None
    model_options = node_options(object_info, "ControlNetLoader", "control_net_name")
    if not model_options:
        return None
    ordered_types = [requested_type] if requested_type in CONTROLNET_TYPES else []
    ordered_types.extend(item for item in ("canny", "openpose", "depth", "lineart") if item not in ordered_types)
    for control_type in ordered_types:
        definition = CONTROLNET_TYPES.get(control_type) or {}
        preprocessor = next((item for item in definition.get("preprocessors", ()) if item in object_info), "")
        if not preprocessor:
            continue
        keywords = tuple(definition.get("keywords", ()))
        if requested_model:
            model_name = resolve_choice(requested_model, model_options, label="controlnet")
            return {
                "type": control_type,
                "preprocessor": preprocessor,
                "model_name": model_name,
                "available_model_count": len(model_options),
            }
        matching = [item for item in model_options if any(keyword in item.lower() for keyword in keywords)]
        if not matching:
            continue
        preferred = [
            item for item in matching
            if "sdxl" in item.lower() or "\\xl" in item.lower() or "_xl" in item.lower() or "control-lora" in item.lower()
        ]
        return {
            "type": control_type,
            "preprocessor": preprocessor,
            "model_name": (preferred or matching)[0],
            "available_model_count": len(matching),
        }
    return None


def file_input_name(ref: dict) -> str:
    subfolder = str(ref.get("subfolder") or "").strip().strip("/\\")
    filename = str(ref.get("filename") or "").strip()
    return f"{subfolder}/{filename}" if subfolder else filename


def base_nodes(args, model_name: str) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model_name}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": args.prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": args.negative_prompt, "clip": ["1", 1]}},
    }


def attach_controlnet(workflow: dict, object_info: dict, controlnet: dict, *, positive_ref, negative_ref, image_ref, vae_ref) -> tuple[list, list]:
    preprocessor_id = str(max(int(item) for item in workflow) + 1)
    preprocessor_inputs = required_default_inputs(object_info, controlnet["preprocessor"], {"image": image_ref})
    workflow[preprocessor_id] = {"class_type": controlnet["preprocessor"], "inputs": preprocessor_inputs}
    loader_id = str(int(preprocessor_id) + 1)
    workflow[loader_id] = {
        "class_type": "ControlNetLoader",
        "inputs": {"control_net_name": controlnet["model_name"]},
    }
    apply_id = str(int(loader_id) + 1)
    apply_inputs = {
        "positive": positive_ref,
        "negative": negative_ref,
        "control_net": [loader_id, 0],
        "image": [preprocessor_id, 0],
        "strength": float(controlnet.get("strength", 0.8)),
        "start_percent": float(controlnet.get("start_percent", 0.0)),
        "end_percent": float(controlnet.get("end_percent", 1.0)),
    }
    if object_input_meta(object_info, "ControlNetApplyAdvanced", "vae") is not None:
        apply_inputs["vae"] = vae_ref
    workflow[apply_id] = {
        "class_type": "ControlNetApplyAdvanced",
        "inputs": required_default_inputs(object_info, "ControlNetApplyAdvanced", apply_inputs),
    }
    return [apply_id, 0], [apply_id, 1]


def build_txt2img(args, object_info: dict, model_name: str, *, prompt: str, prefix: str) -> dict:
    workflow = base_nodes(args, model_name)
    workflow["2"]["inputs"]["text"] = prompt
    workflow["4"] = {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": int(args.width), "height": int(args.height), "batch_size": 1},
    }
    workflow["5"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": ["1", 0],
            "positive": ["2", 0],
            "negative": ["3", 0],
            "latent_image": ["4", 0],
            "seed": int(args.seed),
            "steps": int(args.steps),
            "cfg": float(args.cfg),
            "sampler_name": args.sampler,
            "scheduler": args.scheduler,
            "denoise": 1,
        },
    }
    workflow["6"] = {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}}
    workflow["7"] = {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": prefix}}
    return workflow


def build_img2img(
    args,
    object_info: dict,
    model_name: str,
    *,
    source_ref: dict,
    prompt: str,
    denoise: float,
    prefix: str,
    controlnet: dict | None = None,
) -> dict:
    workflow = base_nodes(args, model_name)
    workflow["2"]["inputs"]["text"] = prompt
    workflow["4"] = {
        "class_type": "LoadImage",
        "inputs": {"image": file_input_name(source_ref), "upload": "image"},
    }
    workflow["5"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["4", 0], "vae": ["1", 2]}}
    positive_ref = ["2", 0]
    negative_ref = ["3", 0]
    if controlnet:
        positive_ref, negative_ref = attach_controlnet(
            workflow,
            object_info,
            controlnet,
            positive_ref=positive_ref,
            negative_ref=negative_ref,
            image_ref=["4", 0],
            vae_ref=["1", 2],
        )
    sampler_id = str(max(int(item) for item in workflow) + 1)
    workflow[sampler_id] = {
        "class_type": "KSampler",
        "inputs": {
            "model": ["1", 0],
            "positive": positive_ref,
            "negative": negative_ref,
            "latent_image": ["5", 0],
            "seed": int(args.seed) + len(workflow),
            "steps": int(args.steps),
            "cfg": float(args.cfg),
            "sampler_name": args.sampler,
            "scheduler": args.scheduler,
            "denoise": float(denoise),
        },
    }
    decode_id = str(int(sampler_id) + 1)
    save_id = str(int(sampler_id) + 2)
    workflow[decode_id] = {"class_type": "VAEDecode", "inputs": {"samples": [sampler_id, 0], "vae": ["1", 2]}}
    workflow[save_id] = {"class_type": "SaveImage", "inputs": {"images": [decode_id, 0], "filename_prefix": prefix}}
    return workflow


def build_inpaint(
    args,
    object_info: dict,
    model_name: str,
    *,
    source_ref: dict,
    mask_ref: dict,
    prompt: str,
    denoise: float,
    prefix: str,
) -> dict:
    workflow = base_nodes(args, model_name)
    workflow["2"]["inputs"]["text"] = prompt
    workflow["4"] = {"class_type": "LoadImage", "inputs": {"image": file_input_name(source_ref), "upload": "image"}}
    workflow["5"] = {"class_type": "LoadImageMask", "inputs": {"image": file_input_name(mask_ref), "channel": "red"}}
    method = selected_inpaint_method(args, object_info)
    if method == "conditioning":
        if not has_node(object_info, "InpaintModelConditioning"):
            raise ProbeError("InpaintModelConditioning is not available on this ComfyUI instance")
        workflow["6"] = {
            "class_type": "InpaintModelConditioning",
            "inputs": required_default_inputs(
                object_info,
                "InpaintModelConditioning",
                {
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "vae": ["1", 2],
                    "pixels": ["4", 0],
                    "mask": ["5", 0],
                    "noise_mask": bool(args.inpaint_noise_mask),
                },
            ),
        }
        positive_ref = ["6", 0]
        negative_ref = ["6", 1]
        latent_ref = ["6", 2]
    else:
        workflow["6"] = {
            "class_type": "VAEEncodeForInpaint",
            "inputs": required_default_inputs(
                object_info,
                "VAEEncodeForInpaint",
                {"pixels": ["4", 0], "mask": ["5", 0], "vae": ["1", 2], "grow_mask_by": 6},
            ),
        }
        positive_ref = ["2", 0]
        negative_ref = ["3", 0]
        latent_ref = ["6", 0]
    model_ref = maybe_apply_differential_diffusion(args, object_info, workflow, ["1", 0])
    sampler_id = str(max(int(item) for item in workflow) + 1)
    workflow[sampler_id] = {
        "class_type": "KSampler",
        "inputs": {
            "model": model_ref,
            "positive": positive_ref,
            "negative": negative_ref,
            "latent_image": latent_ref,
            "seed": int(args.seed) + 107,
            "steps": int(args.steps),
            "cfg": float(args.cfg),
            "sampler_name": args.sampler,
            "scheduler": args.scheduler,
            "denoise": float(denoise),
        },
    }
    decode_id = str(int(sampler_id) + 1)
    save_id = str(int(sampler_id) + 2)
    workflow[decode_id] = {"class_type": "VAEDecode", "inputs": {"samples": [sampler_id, 0], "vae": ["1", 2]}}
    workflow[save_id] = {"class_type": "SaveImage", "inputs": {"images": [decode_id, 0], "filename_prefix": prefix}}
    return workflow


def build_outpaint(
    args,
    object_info: dict,
    model_name: str,
    *,
    source_ref: dict,
    source_size: tuple[int, int],
    prompt: str,
    prefix: str,
) -> dict:
    workflow = base_nodes(args, model_name)
    workflow["2"]["inputs"]["text"] = prompt
    workflow["4"] = {"class_type": "LoadImage", "inputs": {"image": file_input_name(source_ref), "upload": "image"}}
    padding = outpaint_padding(args)
    workflow["5"] = {
        "class_type": "ImagePadForOutpaint",
        "inputs": required_default_inputs(
            object_info,
            "ImagePadForOutpaint",
            {
                "image": ["4", 0],
                **padding,
                "feathering": int(args.outpaint_feathering),
            },
        ),
    }
    padded_pixels_ref = ["5", 0]
    mask_ref = ["5", 1]
    next_id = 6
    if outpaint_seam_prefill_enabled(args, object_info):
        loader_id = str(next_id)
        prefill_id = str(next_id + 1)
        next_id += 2
        workflow[loader_id] = {
            "class_type": "INPAINT_LoadInpaintModel",
            "inputs": required_default_inputs(
                object_info,
                "INPAINT_LoadInpaintModel",
                {"model_name": str(args.outpaint_prefill_model)},
            ),
        }
        workflow[prefill_id] = {
            "class_type": "INPAINT_InpaintWithModel",
            "inputs": required_default_inputs(
                object_info,
                "INPAINT_InpaintWithModel",
                {
                    "inpaint_model": [loader_id, 0],
                    "image": padded_pixels_ref,
                    "mask": mask_ref,
                    "seed": int(args.seed) + 197,
                },
            ),
        }
        padded_pixels_ref = [prefill_id, 0]
    method = selected_outpaint_method(args, object_info)
    if method == "conditioning":
        if not has_node(object_info, "InpaintModelConditioning"):
            raise ProbeError("InpaintModelConditioning is not available on this ComfyUI instance")
        conditioning_id = str(next_id)
        workflow[conditioning_id] = {
            "class_type": "InpaintModelConditioning",
            "inputs": required_default_inputs(
                object_info,
                "InpaintModelConditioning",
                {
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "vae": ["1", 2],
                    "pixels": padded_pixels_ref,
                    "mask": mask_ref,
                    "noise_mask": bool(args.inpaint_noise_mask),
                },
            ),
        }
        positive_ref = [conditioning_id, 0]
        negative_ref = [conditioning_id, 1]
        latent_ref = [conditioning_id, 2]
    else:
        encode_id = str(next_id)
        if method == "full_redraw":
            workflow[encode_id] = {
                "class_type": "VAEEncode",
                "inputs": {"pixels": padded_pixels_ref, "vae": ["1", 2]},
            }
        else:
            workflow[encode_id] = {
                "class_type": "VAEEncodeForInpaint",
                "inputs": required_default_inputs(
                    object_info,
                    "VAEEncodeForInpaint",
                    {"pixels": padded_pixels_ref, "mask": mask_ref, "vae": ["1", 2], "grow_mask_by": 6},
                ),
            }
        positive_ref = ["2", 0]
        negative_ref = ["3", 0]
        latent_ref = [encode_id, 0]
    model_ref = maybe_apply_differential_diffusion(args, object_info, workflow, ["1", 0])
    sampler_id = str(max(int(item) for item in workflow) + 1)
    workflow[sampler_id] = {
        "class_type": "KSampler",
        "inputs": {
            "model": model_ref,
            "positive": positive_ref,
            "negative": negative_ref,
            "latent_image": latent_ref,
            "seed": int(args.seed) + 211,
            "steps": int(args.steps),
            "cfg": float(args.cfg),
            "sampler_name": args.sampler,
            "scheduler": args.scheduler,
            "denoise": float(args.outpaint_denoise),
        },
    }
    decode_id = str(int(sampler_id) + 1)
    save_id = str(int(sampler_id) + 2)
    workflow[decode_id] = {"class_type": "VAEDecode", "inputs": {"samples": [sampler_id, 0], "vae": ["1", 2]}}
    output_ref = [decode_id, 0]
    if bool(args.outpaint_preserve_source):
        mask_id = str(int(sampler_id) + 2)
        feather_id = str(int(sampler_id) + 3)
        composite_id = str(int(sampler_id) + 4)
        save_id = str(int(sampler_id) + 5)
        # Keep the original interior stable and use a soft transition into the
        # generated outer area.  VAE decoding the entire padded latent otherwise
        # frequently introduces a visible rectangular source boundary.
        source_feather = max(1, min(int(args.outpaint_source_feather), min(source_size) // 3))
        workflow[mask_id] = {
            "class_type": "SolidMask",
            "inputs": {"value": 1.0, "width": int(source_size[0]), "height": int(source_size[1])},
        }
        workflow[feather_id] = {
            "class_type": "FeatherMask",
            "inputs": {
                "mask": [mask_id, 0],
                "left": source_feather,
                "top": source_feather,
                "right": source_feather,
                "bottom": source_feather,
            },
        }
        workflow[composite_id] = {
            "class_type": "ImageCompositeMasked",
            "inputs": {
                "destination": [decode_id, 0],
                "source": ["4", 0],
                "x": int(padding["left"]),
                "y": int(padding["top"]),
                "resize_source": False,
                "mask": [feather_id, 0],
            },
        }
        output_ref = [composite_id, 0]
    workflow[save_id] = {"class_type": "SaveImage", "inputs": {"images": output_ref, "filename_prefix": prefix}}
    return workflow


def build_upscale_redraw(
    args,
    object_info: dict,
    model_name: str,
    *,
    source_ref: dict,
    source_size: tuple[int, int],
    prompt: str,
    prefix: str,
) -> dict:
    target_width, target_height = scaled_dimensions(*source_size, float(args.upscale_factor))
    workflow = base_nodes(args, model_name)
    workflow["2"]["inputs"]["text"] = prompt
    workflow["4"] = {"class_type": "LoadImage", "inputs": {"image": file_input_name(source_ref), "upload": "image"}}
    workflow["5"] = {
        "class_type": "ImageScale",
        "inputs": required_default_inputs(
            object_info,
            "ImageScale",
            {
                "image": ["4", 0],
                "upscale_method": "lanczos",
                "width": target_width,
                "height": target_height,
                "crop": "disabled",
            },
        ),
    }
    workflow["6"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["1", 2]}}
    workflow["7"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": ["1", 0],
            "positive": ["2", 0],
            "negative": ["3", 0],
            "latent_image": ["6", 0],
            "seed": int(args.seed) + 307,
            "steps": int(args.steps),
            "cfg": float(args.cfg),
            "sampler_name": args.sampler,
            "scheduler": args.scheduler,
            "denoise": float(args.upscale_denoise),
        },
    }
    workflow["8"] = {"class_type": "VAEDecode", "inputs": {"samples": ["7", 0], "vae": ["1", 2]}}
    workflow["9"] = {"class_type": "SaveImage", "inputs": {"images": ["8", 0], "filename_prefix": prefix}}
    return workflow


def build_two_image_blend(args, object_info: dict, model_name: str, *, source_ref: dict, blend_ref: dict, prompt: str, prefix: str) -> dict:
    """Semantically combine references without creating a ghosted pixel overlay."""

    if not is_sdxl_checkpoint_name(model_name):
        raise ProbeError("IPAdapter Style & Composition requires an explicitly identified SDXL checkpoint")
    assets = require_sdxl_ipadapter_assets(object_info)
    blend_factor = max(0.0, min(1.0, float(args.blend_factor)))
    style_weight = 0.4 + (0.9 * blend_factor)
    composition_weight = 0.4 + (0.9 * (1.0 - blend_factor))
    workflow = base_nodes(args, model_name)
    workflow["2"]["inputs"]["text"] = prompt
    workflow["4"] = {
        "class_type": "IPAdapterModelLoader",
        "inputs": required_default_inputs(
            object_info,
            "IPAdapterModelLoader",
            {"ipadapter_file": assets["ipadapter_file"]},
        ),
    }
    workflow["5"] = {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": assets["clip_name"]}}
    workflow["6"] = {"class_type": "LoadImage", "inputs": {"image": file_input_name(blend_ref), "upload": "image"}}
    workflow["7"] = {"class_type": "LoadImage", "inputs": {"image": file_input_name(source_ref), "upload": "image"}}
    workflow["8"] = {
        "class_type": "IPAdapterStyleComposition",
        "inputs": required_default_inputs(
            object_info,
            "IPAdapterStyleComposition",
            {
                "model": ["1", 0],
                "ipadapter": ["4", 0],
                "clip_vision": ["5", 0],
                "image_style": ["6", 0],
                "image_composition": ["7", 0],
                "weight_style": style_weight,
                "weight_composition": composition_weight,
                "expand_style": False,
                "combine_embeds": "average",
                "start_at": 0.0,
                "end_at": 1.0,
                "embeds_scaling": "V only",
            },
        ),
    }
    workflow["9"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["7", 0], "vae": ["1", 2]}}
    workflow["10"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": ["8", 0],
            "positive": ["2", 0],
            "negative": ["3", 0],
            "latent_image": ["9", 0],
            "seed": int(args.seed) + 409,
            "steps": int(args.steps),
            "cfg": float(args.cfg),
            "sampler_name": args.sampler,
            "scheduler": args.scheduler,
            "denoise": float(args.blend_denoise),
        },
    }
    workflow["11"] = {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["1", 2]}}
    workflow["12"] = {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": prefix}}
    return workflow


def build_ipadapter_style_reference(
    args,
    object_info: dict,
    model_name: str,
    *,
    source_ref: dict,
    style_ref: dict,
    prompt: str,
    prefix: str,
) -> dict:
    if not is_sdxl_checkpoint_name(model_name):
        raise ProbeError("IPAdapter Style & Composition requires an explicitly identified SDXL checkpoint")
    assets = require_sdxl_ipadapter_assets(object_info)
    workflow = base_nodes(args, model_name)
    workflow["2"]["inputs"]["text"] = prompt
    workflow["4"] = {
        "class_type": "IPAdapterModelLoader",
        "inputs": required_default_inputs(
            object_info,
            "IPAdapterModelLoader",
            {"ipadapter_file": assets["ipadapter_file"]},
        ),
    }
    workflow["5"] = {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": assets["clip_name"]}}
    workflow["6"] = {"class_type": "LoadImage", "inputs": {"image": file_input_name(style_ref), "upload": "image"}}
    workflow["7"] = {"class_type": "LoadImage", "inputs": {"image": file_input_name(source_ref), "upload": "image"}}
    workflow["8"] = {
        "class_type": "IPAdapterStyleComposition",
        "inputs": required_default_inputs(
            object_info,
            "IPAdapterStyleComposition",
            {
                "model": ["1", 0],
                "ipadapter": ["4", 0],
                "clip_vision": ["5", 0],
                "image_style": ["6", 0],
                "image_composition": ["7", 0],
                "weight_style": float(args.ipadapter_style_weight),
                "weight_composition": float(args.ipadapter_composition_weight),
                "expand_style": False,
                "combine_embeds": "average",
                "start_at": 0.0,
                "end_at": 1.0,
                "embeds_scaling": "V only",
            },
        ),
    }
    workflow["9"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["7", 0], "vae": ["1", 2]}}
    workflow["10"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": ["8", 0],
            "positive": ["2", 0],
            "negative": ["3", 0],
            "latent_image": ["9", 0],
            "seed": int(args.seed) + 503,
            "steps": int(args.steps),
            "cfg": float(args.cfg),
            "sampler_name": args.sampler,
            "scheduler": args.scheduler,
            "denoise": float(args.ipadapter_denoise),
        },
    }
    workflow["11"] = {"class_type": "VAEDecode", "inputs": {"samples": ["10", 0], "vae": ["1", 2]}}
    workflow["12"] = {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": prefix}}
    return workflow


def build_ipadapter_inpaint_reference(
    args,
    object_info: dict,
    model_name: str,
    *,
    source_ref: dict,
    mask_ref: dict,
    style_ref: dict,
    prompt: str,
    denoise: float,
    prefix: str,
) -> dict:
    if not is_sdxl_checkpoint_name(model_name):
        raise ProbeError("IPAdapter Style & Composition requires an explicitly identified SDXL checkpoint")
    assets = require_sdxl_ipadapter_assets(object_info)
    workflow = base_nodes(args, model_name)
    workflow["2"]["inputs"]["text"] = prompt
    workflow["4"] = {
        "class_type": "IPAdapterModelLoader",
        "inputs": required_default_inputs(
            object_info,
            "IPAdapterModelLoader",
            {"ipadapter_file": assets["ipadapter_file"]},
        ),
    }
    workflow["5"] = {"class_type": "CLIPVisionLoader", "inputs": {"clip_name": assets["clip_name"]}}
    workflow["6"] = {"class_type": "LoadImage", "inputs": {"image": file_input_name(style_ref), "upload": "image"}}
    workflow["7"] = {"class_type": "LoadImage", "inputs": {"image": file_input_name(source_ref), "upload": "image"}}
    workflow["8"] = {"class_type": "LoadImageMask", "inputs": {"image": file_input_name(mask_ref), "channel": "red"}}
    workflow["9"] = {
        "class_type": "IPAdapterStyleComposition",
        "inputs": required_default_inputs(
            object_info,
            "IPAdapterStyleComposition",
            {
                "model": ["1", 0],
                "ipadapter": ["4", 0],
                "clip_vision": ["5", 0],
                "image_style": ["6", 0],
                "image_composition": ["7", 0],
                "weight_style": float(args.ipadapter_style_weight),
                "weight_composition": float(args.ipadapter_composition_weight),
                "expand_style": False,
                "combine_embeds": "average",
                "start_at": 0.0,
                "end_at": 1.0,
                "embeds_scaling": "V only",
            },
        ),
    }
    method = selected_inpaint_method(args, object_info)
    if method == "conditioning":
        if not has_node(object_info, "InpaintModelConditioning"):
            raise ProbeError("InpaintModelConditioning is not available on this ComfyUI instance")
        workflow["10"] = {
            "class_type": "InpaintModelConditioning",
            "inputs": required_default_inputs(
                object_info,
                "InpaintModelConditioning",
                {
                    "positive": ["2", 0],
                    "negative": ["3", 0],
                    "vae": ["1", 2],
                    "pixels": ["7", 0],
                    "mask": ["8", 0],
                    "noise_mask": bool(args.inpaint_noise_mask),
                },
            ),
        }
        positive_ref = ["10", 0]
        negative_ref = ["10", 1]
        latent_ref = ["10", 2]
    else:
        workflow["10"] = {
            "class_type": "VAEEncodeForInpaint",
            "inputs": required_default_inputs(
                object_info,
                "VAEEncodeForInpaint",
                {"pixels": ["7", 0], "mask": ["8", 0], "vae": ["1", 2], "grow_mask_by": 6},
            ),
        }
        positive_ref = ["2", 0]
        negative_ref = ["3", 0]
        latent_ref = ["10", 0]
    model_ref = maybe_apply_differential_diffusion(args, object_info, workflow, ["9", 0])
    sampler_id = str(max(int(item) for item in workflow) + 1)
    workflow[sampler_id] = {
        "class_type": "KSampler",
        "inputs": {
            "model": model_ref,
            "positive": positive_ref,
            "negative": negative_ref,
            "latent_image": latent_ref,
            "seed": int(args.seed) + 607,
            "steps": int(args.steps),
            "cfg": float(args.cfg),
            "sampler_name": args.sampler,
            "scheduler": args.scheduler,
            "denoise": float(denoise),
        },
    }
    decode_id = str(int(sampler_id) + 1)
    save_id = str(int(sampler_id) + 2)
    workflow[decode_id] = {"class_type": "VAEDecode", "inputs": {"samples": [sampler_id, 0], "vae": ["1", 2]}}
    workflow[save_id] = {"class_type": "SaveImage", "inputs": {"images": [decode_id, 0], "filename_prefix": prefix}}
    return workflow


def case_enabled(args, case_id: str) -> bool:
    selected = str(getattr(args, "only_case", "") or "").strip()
    return not selected or selected == case_id


def case_denoise(args, default: float) -> float:
    override = float(getattr(args, "case_denoise", 0.0) or 0.0)
    return override if override > 0 else float(default)


def case_prompt_suite(args) -> dict:
    if str(args.prompt_suite or "").strip().lower() == "legacy_2girls":
        source = LEGACY_2GIRLS_PROMPT
        prompts = {
            "source": source,
            "redraw": f"{source}, warm sunset lighting, polished anime illustration",
            "style": f"{source}, soft watercolor anime illustration, pastel wash, paper texture",
            "feature": f"{source}, same two adult cat girls, same pose, same maid uniforms, refined lineart",
            "inpaint_remove": f"{source}, remove the masked object, seamless repair, keep the two adult cat girls consistent",
            "inpaint_replace": f"{source}, a blue beach umbrella and small seashells in the masked area, consistent anime lighting",
            "outpaint": f"{source}, continue the same anime scene outward, seamless extension",
            "controlnet": f"{source}, same controlled composition, black maid uniforms, crisp lineart",
            "upscale_redraw": f"{source}, high detail anime style, clean refined redraw, preserve composition",
            "blend": f"{source}, merge the indoor source image with the second reference image, coherent anime illustration, preserve the two adult cat girls and kissing action",
            "style_reference": f"{source}, imitate the style reference while preserving the source composition and two adult cat girls",
            "ipadapter_inpaint": f"{source}, replace only the masked clothing with an elegant patterned kimono, use the separate style reference for fabric color and painterly texture, preserve faces and pose",
        }
    else:
        prompts = {
        "source": args.source_prompt,
        "redraw": "anime style, adult cat girl laying on the beach at sunset, same pose, warm orange sky, polished illustration",
        "style": "soft watercolor anime illustration, adult cat girl laying on the beach, pastel wash, paper texture, same composition",
        "feature": "clean detailed anime style, same adult cat girl, same pose, same cat ears, same beach layout, refined lineart",
        "inpaint_remove": "clean empty beach sand and ocean background, remove the object in the masked area, seamless repair, anime style",
        "inpaint_replace": "a blue beach umbrella and small seashells in the masked area, consistent anime beach lighting",
        "outpaint": "continue the sunny anime beach scene outward, ocean horizon, sand, blue sky, seamless extension",
        "controlnet": "anime style, adult cat girl in the same lying pose on the beach, black bikini, crisp lineart, controlled composition",
        "upscale_redraw": "high detail anime style, same adult cat girl on the beach, clean refined upscale redraw, preserve composition",
        "blend": "anime style, blend the source character pose with the second reference image, coherent scene, clean lineart",
        "style_reference": "anime style, imitate the style reference image while preserving the source composition, clean lineart",
        "ipadapter_inpaint": "anime style, replace only the masked clothing using the separate style reference, preserve face, pose, and background",
        }
    override = str(getattr(args, "case_prompt", "") or "").strip()
    if override:
        case_key = str(getattr(args, "only_case", "") or "").strip()
        mapping = {
            "img2img_redraw_sunset": "redraw",
            "img2img_style_watercolor": "style",
            "img2img_feature_preserve": "feature",
            "inpaint_remove_repair": "inpaint_remove",
            "inpaint_replace_edit": "inpaint_replace",
            "outpaint_expand_beach": "outpaint",
            "upscale_redraw_imagescale": "upscale_redraw",
            "two_image_blend_mix": "blend",
            "ipadapter_style_reference": "style_reference",
            "ipadapter_inpaint_reference": "ipadapter_inpaint",
        }
        if case_key.startswith("controlnet_copy_composition_"):
            prompts["controlnet"] = override
        elif case_key in mapping:
            prompts[mapping[case_key]] = override
    return prompts


def queue_and_fetch(client: ComfyClient, workflow: dict, out_png: Path, *, max_seconds: int, poll_seconds: float, timeout: int) -> dict:
    client_id = uuid.uuid4().hex
    submitted_at = time.perf_counter()
    prompt = client.json("/prompt", method="POST", payload={"prompt": workflow, "client_id": client_id}, timeout=timeout)
    prompt_id = str(prompt.get("prompt_id") or "").strip()
    if not prompt_id:
        raise ProbeError(f"ComfyUI did not return prompt_id: {prompt}")
    last_history = {}
    while time.perf_counter() - submitted_at <= int(max_seconds):
        history = client.json(f"/history/{urllib.parse.quote(prompt_id)}", timeout=timeout)
        last_history = history
        item = history.get(prompt_id) if isinstance(history.get(prompt_id), dict) else None
        if item:
            status = item.get("status") if isinstance(item.get("status"), dict) else {}
            if str(status.get("status_str") or "").lower() == "error":
                raise ProbeError(f"ComfyUI prompt {prompt_id} failed: {json.dumps(status, ensure_ascii=False)[:1200]}")
            outputs = item.get("outputs") if isinstance(item.get("outputs"), dict) else {}
            for output in outputs.values():
                images = output.get("images") if isinstance(output, dict) and isinstance(output.get("images"), list) else []
                if not images:
                    continue
                image = images[0]
                query = urllib.parse.urlencode({
                    "filename": image.get("filename") or "",
                    "subfolder": image.get("subfolder") or "",
                    "type": image.get("type") or "output",
                })
                data = client.bytes(f"/view?{query}", timeout=timeout)
                out_png.write_bytes(data)
                return {
                    "prompt_id": prompt_id,
                    "image_ref": image,
                    "path": str(out_png),
                    "size_bytes": out_png.stat().st_size,
                    "seconds": round(time.perf_counter() - submitted_at, 3),
                }
        time.sleep(max(0.5, float(poll_seconds)))
    raise ProbeError(f"timeout waiting for ComfyUI prompt {prompt_id}; last_history={json.dumps(last_history, ensure_ascii=False)[:1200]}")


def create_mask(path: Path, width: int, height: int, *, shape: str = "default") -> None:
    from PIL import Image, ImageDraw

    mask = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(mask)
    draw_mask_shape(draw, width, height, shape)
    mask.save(path)


def draw_mask_shape(draw, width: int, height: int, shape: str) -> None:
    shape = str(shape or "default").strip().lower()
    if shape == "window":
        polygon = [
            (int(width * 0.42), int(height * 0.00)),
            (int(width * 0.89), int(height * 0.00)),
            (int(width * 0.81), int(height * 0.20)),
            (int(width * 0.36), int(height * 0.17)),
        ]
        draw.polygon(polygon, fill=(255, 255, 255, 255))
        return
    if shape == "background_wall":
        draw.rectangle(
            (int(width * 0.04), int(height * 0.03), int(width * 0.28), int(height * 0.24)),
            fill=(255, 255, 255, 255),
        )
        return
    if shape == "small_wall":
        draw.ellipse(
            (int(width * 0.06), int(height * 0.06), int(width * 0.19), int(height * 0.19)),
            fill=(255, 255, 255, 255),
        )
        return
    if shape == "kimono_clothes":
        polygons = [
            [
                (int(width * 0.39), int(height * 0.45)),
                (int(width * 0.67), int(height * 0.43)),
                (int(width * 0.95), int(height * 0.55)),
                (int(width * 1.00), int(height * 0.96)),
                (int(width * 0.58), int(height * 1.00)),
                (int(width * 0.39), int(height * 0.78)),
            ],
            [
                (int(width * 0.13), int(height * 0.56)),
                (int(width * 0.50), int(height * 0.55)),
                (int(width * 0.74), int(height * 0.75)),
                (int(width * 0.55), int(height * 1.00)),
                (int(width * 0.04), int(height * 0.91)),
            ],
            [
                (int(width * 0.00), int(height * 0.54)),
                (int(width * 0.28), int(height * 0.56)),
                (int(width * 0.44), int(height * 0.74)),
                (int(width * 0.25), int(height * 0.96)),
                (int(width * 0.00), int(height * 0.90)),
            ],
            [
                (int(width * 0.62), int(height * 0.66)),
                (int(width * 1.00), int(height * 0.62)),
                (int(width * 1.00), int(height * 1.00)),
                (int(width * 0.70), int(height * 1.00)),
            ],
            [
                (int(width * 0.30), int(height * 0.86)),
                (int(width * 0.73), int(height * 0.84)),
                (int(width * 0.83), int(height * 1.00)),
                (int(width * 0.24), int(height * 1.00)),
            ],
        ]
        for polygon in polygons:
            draw.polygon(polygon, fill=(255, 255, 255, 255))
        return
    x0 = int(width * 0.58)
    y0 = int(height * 0.52)
    x1 = int(width * 0.92)
    y1 = int(height * 0.88)
    draw.ellipse((x0, y0, x1, y1), fill=(255, 255, 255, 255))


def create_synthetic_source(path: Path, width: int, height: int) -> None:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), (126, 199, 235))
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, int(height * 0.45), width, height), fill=(238, 211, 145))
    draw.rectangle((0, int(height * 0.35), width, int(height * 0.47)), fill=(58, 151, 195))
    draw.ellipse((int(width * 0.34), int(height * 0.35), int(width * 0.58), int(height * 0.70)), fill=(240, 189, 164))
    draw.ellipse((int(width * 0.41), int(height * 0.22), int(width * 0.53), int(height * 0.34)), fill=(242, 198, 176))
    draw.polygon(
        [(int(width * 0.43), int(height * 0.22)), (int(width * 0.40), int(height * 0.13)), (int(width * 0.48), int(height * 0.21))],
        fill=(70, 55, 62),
    )
    draw.polygon(
        [(int(width * 0.51), int(height * 0.22)), (int(width * 0.57), int(height * 0.13)), (int(width * 0.54), int(height * 0.25))],
        fill=(70, 55, 62),
    )
    draw.ellipse((int(width * 0.64), int(height * 0.58), int(width * 0.83), int(height * 0.77)), fill=(210, 32, 48))
    draw.line((int(width * 0.66), int(height * 0.66), int(width * 0.82), int(height * 0.66)), fill=(255, 255, 255), width=max(2, width // 160))
    draw.line((int(width * 0.74), int(height * 0.59), int(width * 0.74), int(height * 0.77)), fill=(255, 255, 255), width=max(2, width // 160))
    img.save(path)


def image_stats(path: Path) -> dict:
    from PIL import Image, ImageStat

    with Image.open(path) as img:
        rgb = img.convert("RGB")
        stat = ImageStat.Stat(rgb)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "width": rgb.width,
            "height": rgb.height,
            "mode": img.mode,
            "sha256": digest,
            "mean_rgb": [round(float(item), 2) for item in stat.mean],
            "extrema": stat.extrema,
            "size_bytes": path.stat().st_size,
        }


def diff_metrics(source: Path, output: Path, *, mask: Path | None = None) -> dict:
    from PIL import Image, ImageChops, ImageStat

    with Image.open(source) as src_raw, Image.open(output) as out_raw:
        src = src_raw.convert("RGB")
        out = out_raw.convert("RGB")
        if out.size != src.size:
            out_cmp = out.resize(src.size)
        else:
            out_cmp = out
        diff = ImageChops.difference(src, out_cmp).convert("L")
        total = diff.width * diff.height
        changed = diff.point(lambda pixel: 255 if pixel > 12 else 0)
        changed_count = total - changed.histogram()[0]
        payload = {
            "changed_ratio": round(changed_count / max(1, total), 4),
            "mean_abs_luma_delta": round(float(ImageStat.Stat(diff).mean[0]), 3),
            "compared_size": list(src.size),
        }
        if mask and mask.exists():
            with Image.open(mask) as mask_raw:
                alpha = mask_raw.convert("RGBA").getchannel("A").resize(src.size)
                masked_pixels = sum(alpha.point(lambda pixel: 1 if pixel > 16 else 0).histogram()[1:])
                if masked_pixels:
                    masked = ImageChops.multiply(changed, alpha.point(lambda pixel: 255 if pixel > 16 else 0))
                    masked_changed = sum(masked.point(lambda pixel: 1 if pixel > 0 else 0).histogram()[1:])
                    inverse = alpha.point(lambda pixel: 0 if pixel > 16 else 255)
                    unmasked = ImageChops.multiply(changed, inverse)
                    unmasked_changed = sum(unmasked.point(lambda pixel: 1 if pixel > 0 else 0).histogram()[1:])
                    payload.update({
                        "masked_changed_ratio": round(masked_changed / max(1, masked_pixels), 4),
                        "unmasked_changed_ratio": round(unmasked_changed / max(1, total - masked_pixels), 4),
                    })
        return payload


def latent_aligned_dimensions(expected: tuple[int, int], *, multiple: int = 8) -> tuple[int, int]:
    """Return the decoded latent size that ComfyUI can actually emit.

    ``ImagePadForOutpaint`` accepts source images whose dimensions are not a
    multiple of the VAE stride.  The subsequent VAE encode/decode path crops
    the right/bottom edge to the closest lower latent grid instead of adding
    pixels.  Treating that documented/observed alignment as a hard failure
    hid the useful border-quality check behind a false dimension failure.
    """

    step = max(1, int(multiple or 1))
    width = max(1, int(expected[0]))
    height = max(1, int(expected[1]))
    return (
        max(step, (width // step) * step),
        max(step, (height // step) * step),
    )


def expected_dimensions_check(output: Path, expected: tuple[int, int], *, latent_alignment: bool = True) -> dict:
    actual_stats = image_stats(output)
    actual = (int(actual_stats["width"]), int(actual_stats["height"]))
    requested = (int(expected[0]), int(expected[1]))
    expected = latent_aligned_dimensions(requested) if latent_alignment else requested
    expected_ratio = expected[0] / max(1, expected[1])
    actual_ratio = actual[0] / max(1, actual[1])
    ratio_error = abs(actual_ratio - expected_ratio) / max(expected_ratio, 1e-9)
    passed = actual == expected and ratio_error <= 0.01
    return {
        "id": "expected_dimensions_and_aspect",
        "passed": passed,
        "requested": list(requested),
        "expected": list(expected),
        "actual": list(actual),
        "latent_alignment": bool(latent_alignment),
        "relative_aspect_error": round(ratio_error, 6),
    }


def outpaint_border_check(source: Path, output: Path, padding: dict[str, int]) -> dict:
    """Reject the common false-green result where outpaint only adds gray canvas."""

    from PIL import Image

    with Image.open(source) as source_raw, Image.open(output) as output_raw:
        source_size = source_raw.size
        image = output_raw.convert("RGB")
        left = max(0, int(padding.get("left", 0)))
        top = max(0, int(padding.get("top", 0)))
        right = max(0, int(padding.get("right", 0)))
        bottom = max(0, int(padding.get("bottom", 0)))
        requested_size = (source_size[0] + left + right, source_size[1] + top + bottom)
        expected_size = latent_aligned_dimensions(requested_size)
        if image.size != expected_size:
            return {
                "id": "outpaint_generated_border",
                "passed": False,
                "reason": "output dimensions do not match VAE-aligned source plus configured padding",
                "source_size": list(source_size),
                "requested_size": list(requested_size),
                "expected_size": list(expected_size),
                "actual_size": list(image.size),
                "padding": {"left": left, "top": top, "right": right, "bottom": bottom},
            }

        # VAE alignment crops only the trailing edge.  Preserve the requested
        # left/top offsets and calculate the actually decoded right/bottom
        # band so seam probes never include source pixels by mistake.
        effective_right = max(0, image.width - source_size[0] - left)
        effective_bottom = max(0, image.height - source_size[1] - top)
        if image.width < left + source_size[0] or image.height < top + source_size[1]:
            return {
                "id": "outpaint_generated_border",
                "passed": False,
                "reason": "VAE-aligned output no longer contains the requested source placement",
                "source_size": list(source_size),
                "requested_size": list(requested_size),
                "expected_size": list(expected_size),
                "actual_size": list(image.size),
                "padding": {"left": left, "top": top, "right": right, "bottom": bottom},
            }

        # Only inspect the outer half of each newly padded band. Feathering near
        # the original image is allowed, while untouched ImagePadForOutpaint
        # canvas remains close to neutral 127/128 gray in these outer strips.
        probe_boxes = []
        if top:
            probe_boxes.append((0, 0, image.width, max(1, top // 2)))
        if effective_bottom:
            probe_boxes.append((0, image.height - max(1, effective_bottom // 2), image.width, image.height))
        middle_top = top
        middle_bottom = image.height - effective_bottom
        if left and middle_bottom > middle_top:
            probe_boxes.append((0, middle_top, max(1, left // 2), middle_bottom))
        if effective_right and middle_bottom > middle_top:
            probe_boxes.append((image.width - max(1, effective_right // 2), middle_top, image.width, middle_bottom))

        total = 0
        gray_placeholder = 0
        for box in probe_boxes:
            probe = image.crop(box)
            pixels = probe.get_flattened_data() if hasattr(probe, "get_flattened_data") else probe.getdata()
            for red, green, blue in pixels:
                total += 1
                channel_spread = max(red, green, blue) - min(red, green, blue)
                channel_mean = (red + green + blue) / 3.0
                if channel_spread <= 6 and abs(channel_mean - 127.5) <= 8:
                    gray_placeholder += 1
        gray_ratio = gray_placeholder / max(1, total)
        seam_band = max(1, min(32, min(source_size) // 8))
        seam_deltas = {}

        def color_mean(box):
            from PIL import ImageStat

            return ImageStat.Stat(image.crop(box)).mean

        def mean_delta(first, second):
            return sum(abs(a - b) for a, b in zip(first, second)) / 3.0

        if left:
            seam_deltas["left"] = mean_delta(
                color_mean((left - seam_band, top, left, image.height - bottom)),
                color_mean((left, top, left + seam_band, image.height - bottom)),
            )
        if effective_right:
            boundary = image.width - effective_right
            seam_deltas["right"] = mean_delta(
                color_mean((boundary - seam_band, top, boundary, image.height - bottom)),
                color_mean((boundary, top, boundary + seam_band, image.height - bottom)),
            )
        if top:
            seam_deltas["top"] = mean_delta(
                color_mean((left, top - seam_band, image.width - right, top)),
                color_mean((left, top, image.width - right, top + seam_band)),
            )
        if effective_bottom:
            boundary = image.height - effective_bottom
            seam_deltas["bottom"] = mean_delta(
                color_mean((left, boundary - seam_band, image.width - right, boundary)),
                color_mean((left, boundary, image.width - right, boundary + seam_band)),
            )
        seam_threshold = 20.0
        max_seam_delta = max(seam_deltas.values(), default=0.0)
        return {
            "id": "outpaint_generated_border",
            "passed": total > 0 and gray_ratio < 0.80 and max_seam_delta <= seam_threshold,
            "source_size": list(source_size),
            "requested_size": list(requested_size),
            "expected_size": list(expected_size),
            "actual_size": list(image.size),
            "padding": {"left": left, "top": top, "right": right, "bottom": bottom},
            "effective_padding": {"left": left, "top": top, "right": effective_right, "bottom": effective_bottom},
            "outer_probe_pixels": total,
            "neutral_gray_placeholder_ratio": round(gray_ratio, 6),
            "failure_threshold": 0.80,
            "boundary_mean_rgb_delta": {name: round(value, 3) for name, value in seam_deltas.items()},
            "max_boundary_mean_rgb_delta": round(max_seam_delta, 3),
            "seam_failure_threshold": seam_threshold,
        }


def outpaint_center_preservation_check(source: Path, output: Path, padding: dict[str, int], feather: int) -> dict:
    """Verify that outpaint has not redrawn the protected source interior."""

    from PIL import Image, ImageChops, ImageStat

    with Image.open(source) as source_raw, Image.open(output) as output_raw:
        source_image = source_raw.convert("RGB")
        output_image = output_raw.convert("RGB")
        inset = max(1, min(int(feather), source_image.width // 3, source_image.height // 3))
        source_crop = source_image.crop((inset, inset, source_image.width - inset, source_image.height - inset))
        target_crop = output_image.crop((
            int(padding["left"]) + inset,
            int(padding["top"]) + inset,
            int(padding["left"]) + source_image.width - inset,
            int(padding["top"]) + source_image.height - inset,
        ))
        if target_crop.size != source_crop.size:
            return {"id": "outpaint_source_interior_preserved", "passed": False, "reason": "protected output region has the wrong dimensions"}
        diff = ImageChops.difference(source_crop, target_crop)
        mean_delta = sum(ImageStat.Stat(diff).mean) / 3.0
        return {
            "id": "outpaint_source_interior_preserved",
            "passed": mean_delta <= 1.0,
            "inset": inset,
            "mean_abs_rgb_delta": round(mean_delta, 4),
            "failure_threshold": 1.0,
        }


def run_case(client: ComfyClient, args, *, case: dict, workflow: dict, source_path: Path | None, mask_path: Path | None) -> dict:
    started = time.perf_counter()
    out_png = Path(args.out_dir) / f"{case['id']}.png"
    result = {
        "id": case["id"],
        "label": case["label"],
        "status": "fail",
        "semantic_expectation": case.get("semantic_expectation", ""),
        "semantic_verification": {
            "status": "approved_by_operator" if args.approve_semantic_review else "manual_review_required",
            "machine_verified": False,
            "reason": (
                "operator explicitly approved this semantic review"
                if args.approve_semantic_review
                else "workflow completion and numeric image invariants cannot prove prompt-level visual correctness"
            ),
        },
        "notes": case.get("notes", ""),
        "started_at": now_iso(),
    }
    try:
        output = queue_and_fetch(
            client,
            workflow,
            out_png,
            max_seconds=args.max_seconds,
            poll_seconds=args.poll_seconds,
            timeout=args.request_timeout,
        )
        result["status"] = "pass"
        result["output"] = output
        result["image_stats"] = image_stats(out_png)
        if source_path:
            result["diff_metrics"] = diff_metrics(source_path, out_png, mask=mask_path)
        automated_checks = []
        if case.get("expect_larger_than_source") and source_path:
            source_stats = image_stats(source_path)
            automated_checks.append({
                "id": "larger_than_source",
                "passed": result["image_stats"]["width"] > source_stats["width"] or result["image_stats"]["height"] > source_stats["height"],
                "source": {"width": source_stats["width"], "height": source_stats["height"]},
                "output": {"width": result["image_stats"]["width"], "height": result["image_stats"]["height"]},
            })
        if case.get("expected_dimensions"):
            automated_checks.append(expected_dimensions_check(out_png, tuple(case["expected_dimensions"])))
        if case.get("outpaint_padding") and source_path:
            automated_checks.append(outpaint_border_check(source_path, out_png, case["outpaint_padding"]))
            if case.get("outpaint_preserve_source"):
                automated_checks.append(outpaint_center_preservation_check(
                    source_path,
                    out_png,
                    case["outpaint_padding"],
                    int(case.get("outpaint_source_feather", 1)),
                ))
        result["automated_checks"] = automated_checks
        failed_checks = [item for item in automated_checks if not item.get("passed")]
        if failed_checks:
            result["status"] = "fail"
            result["validation_error"] = "machine image invariant failed: " + ", ".join(
                str(item.get("id") or "unknown") for item in failed_checks
            )
    except Exception as exc:
        result["status"] = "fail"
        result["error"] = sanitize_text(exc)
        result["traceback"] = sanitize_text(traceback.format_exc(limit=8))
    finally:
        result["seconds"] = round(time.perf_counter() - started, 3)
        result["finished_at"] = now_iso()
    return result


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def run_matrix(args) -> dict:
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    args.out_dir = str(out_dir)
    object_info_path = out_dir / "object_info_summary.json"
    client = ComfyClient(args.comfyui_url, insecure=args.insecure, timeout=args.request_timeout)
    object_info = client.json("/object_info", timeout=args.request_timeout)
    sampler, scheduler = choose_sampler_settings(object_info, args.sampler, args.scheduler)
    args.sampler = sampler
    args.scheduler = scheduler
    checkpoint_options = node_options(object_info, "CheckpointLoaderSimple", "ckpt_name")
    model_name = resolve_choice(args.model, checkpoint_options, label="checkpoint", allow_fallback=True)
    ipadapter_assets = choose_sdxl_ipadapter_assets(object_info)
    ipadapter_style_compatible = bool(ipadapter_assets and is_sdxl_checkpoint_name(model_name))
    controlnet = choose_controlnet(object_info, args.controlnet_type, args.controlnet_model)
    summary = {
        "available_nodes": {
            name: name in object_info
            for name in (
                "CheckpointLoaderSimple",
                "KSampler",
                "VAEEncode",
                "VAEEncodeForInpaint",
                "ImagePadForOutpaint",
                "SolidMask",
                "FeatherMask",
                "ImageCompositeMasked",
                "ImageScale",
                "IPAdapterUnifiedLoader",
                "IPAdapterModelLoader",
                "CLIPVisionLoader",
                "IPAdapterStyleComposition",
                "ControlNetLoader",
                "ControlNetApplyAdvanced",
                "UpscaleModelLoader",
                "ImageUpscaleWithModel",
            )
        },
        "checkpoint_count": len(checkpoint_options),
        "checkpoint_resolved": model_name,
        "ipadapter_style_composition": {
            "compatible": ipadapter_style_compatible,
            "assets": ipadapter_assets or {},
            "reason": "" if ipadapter_style_compatible else (
                "IPAdapter Style & Composition requires an explicitly identified SDXL checkpoint, "
                "an SDXL adapter file, and a matching ViT-H CLIP Vision model."
            ),
        },
        "sampler": sampler,
        "scheduler": scheduler,
        "controlnet": controlnet or {},
        "upscale_model_options": node_options(object_info, "UpscaleModelLoader", "model_name")[:30],
    }
    write_json(object_info_path, summary)
    report = {
        "ok": False,
        "label": "standalone_comfyui_i2i_matrix",
        "started_at": now_iso(),
        "comfyui_url": args.comfyui_url,
        "model_requested": args.model,
        "model_resolved": model_name,
        "dimensions": {"width": args.width, "height": args.height, "steps": args.steps, "cfg": args.cfg},
        "artifacts": {"out_dir": str(out_dir), "object_info_summary": str(object_info_path)},
        "capabilities": summary,
        "cases": [],
        "skips": [],
        "verification_scope": "workflow execution plus explicit machine image invariants; prompt-level semantics require visual review",
        "backend_generalization": {
            "diffusers": (
                "Generic HF Diffusers can cover txt2img/img2img/inpaint when a repo exposes compatible Diffusers "
                "metadata or a model-card from_pretrained snippet. ControlNet, outpaint, redraw-upscale, and multi-image "
                "blend need pipeline-specific support instead of the current project shortcut."
            ),
            "gguf": (
                "The current hackme_web ComfyUI-GGUF shortcut is intentionally txt2img-only. I2I is theoretically possible "
                "through explicit ComfyUI workflow templates that reuse the GGUF UNet/CLIP/VAE mapping, but each official "
                "GGUF profile needs a separate visual reprobe before exposing it."
            ),
        },
        "unsupported_or_template_only": [
            "True separate-reference style imitation needs IPAdapter/reference/Redux-style nodes or a workflow template; shortcut img2img can only restyle the source image by prompt and denoise.",
            "True separate-reference feature imitation/faces/identity transfer needs reference/adapter nodes and is not a generic shortcut.",
            "Prompt-guided multi-image blending requires IPAdapter/reference nodes or an imported workflow template; direct ImageBlend pixel overlays are rejected as semantically misleading.",
            "The project shortcut upscale path is pure model upscaling. This matrix tests redraw-upscale as a custom ComfyUI workflow via ImageScale + VAEEncode + KSampler.",
        ],
    }
    prompts = case_prompt_suite(args)

    source_path = out_dir / "source_t2i_reference.png"
    imported_source = Path(str(args.source_image_path or "")).expanduser() if args.source_image_path else None
    source_case = {
        "id": "source_t2i_reference",
        "label": "Imported existing source image" if imported_source else "Reference txt2img source",
        "semantic_expectation": (
            "Preserve the imported source bytes and dimensions before starting I2I operations."
            if imported_source
            else "Generate a beach cat-girl source image with a visible editable object region."
        ),
    }
    if imported_source:
        if not imported_source.is_file():
            raise ProbeError(f"--source-image-path does not exist: {imported_source}")
        source_path.write_bytes(imported_source.read_bytes())
        source_result = {
            "id": source_case["id"],
            "label": source_case["label"],
            "status": "pass",
            "semantic_expectation": source_case["semantic_expectation"],
            "output": {
                "path": str(source_path),
                "size_bytes": source_path.stat().st_size,
                "imported_from": str(imported_source),
            },
            "image_stats": image_stats(source_path),
        }
    elif args.synthetic_source_only:
        create_synthetic_source(source_path, args.width, args.height)
        source_result = {
            "id": source_case["id"],
            "label": source_case["label"],
            "status": "pass",
            "semantic_expectation": source_case["semantic_expectation"],
            "output": {"path": str(source_path), "size_bytes": source_path.stat().st_size, "synthetic": True},
            "image_stats": image_stats(source_path),
        }
    else:
        source_workflow = build_txt2img(args, object_info, model_name, prompt=prompts["source"], prefix="hackme_i2i_source")
        source_result = run_case(client, args, case=source_case, workflow=source_workflow, source_path=None, mask_path=None)
        if source_result.get("status") == "pass":
            generated = Path(source_result["output"]["path"])
            if generated != source_path:
                source_path.write_bytes(generated.read_bytes())
                source_result["output"]["reference_copy"] = str(source_path)
        elif args.allow_synthetic_fallback:
            create_synthetic_source(source_path, args.width, args.height)
            source_result["fallback"] = {"synthetic_source": str(source_path), "reason": source_result.get("error", "")}
            source_result["status"] = "pass_with_fallback"
    report["source"] = source_result
    if source_result.get("status") not in {"pass", "pass_with_fallback"}:
        report["ok"] = False
        return report

    source_image_stats = image_stats(source_path)
    source_size = (int(source_image_stats["width"]), int(source_image_stats["height"]))
    report["source_dimensions"] = {"width": source_size[0], "height": source_size[1]}
    source_ref = client.upload_image(source_path, overwrite=True)
    report["artifacts"]["uploaded_source_ref"] = source_ref
    blend_ref = None
    blend_path = None
    if str(getattr(args, "blend_image_path", "") or "").strip():
        blend_path = Path(str(args.blend_image_path)).expanduser()
        if not blend_path.is_file():
            raise ProbeError(f"--blend-image-path does not exist: {blend_path}")
        blend_ref = client.upload_image(blend_path, overwrite=True)
        report["artifacts"]["blend_image"] = str(blend_path)
        report["artifacts"]["uploaded_blend_ref"] = blend_ref
    style_ref = None
    style_path = None
    if str(getattr(args, "style_image_path", "") or "").strip():
        style_path = Path(str(args.style_image_path)).expanduser()
        if not style_path.is_file():
            raise ProbeError(f"--style-image-path does not exist: {style_path}")
        style_ref = client.upload_image(style_path, overwrite=True)
        report["artifacts"]["style_image"] = str(style_path)
        report["artifacts"]["uploaded_style_ref"] = style_ref

    cases = []
    if case_enabled(args, "img2img_redraw_sunset"):
        cases.append({
            "id": "img2img_redraw_sunset",
            "label": "img2img redraw",
            "semantic_expectation": "Keep the beach/cat-girl composition but redraw it as a warm sunset scene.",
            "workflow": build_img2img(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                prompt=prompts["redraw"],
                denoise=case_denoise(args, 0.58),
                prefix="hackme_i2i_redraw",
            ),
        })
    if case_enabled(args, "img2img_style_watercolor"):
        cases.append({
            "id": "img2img_style_watercolor",
            "label": "style imitation by prompt",
            "semantic_expectation": "Restyle the same source into a soft watercolor anime illustration.",
            "notes": "This is prompt-driven source restyling, not separate style-reference imitation.",
            "workflow": build_img2img(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                prompt=prompts["style"],
                denoise=case_denoise(args, 0.46),
                prefix="hackme_i2i_style",
            ),
        })
    if case_enabled(args, "img2img_feature_preserve"):
        cases.append({
            "id": "img2img_feature_preserve",
            "label": "feature imitation from source",
            "semantic_expectation": "Preserve the source pose, cat ears, beach layout, and main character features while cleaning details.",
            "notes": "Feature preservation uses low denoise img2img; identity/reference transfer is template-only.",
            "workflow": build_img2img(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                prompt=prompts["feature"],
                denoise=case_denoise(args, 0.32),
                prefix="hackme_i2i_features",
            ),
        })
    inpaint_enabled = any(
        case_enabled(args, case_id)
        for case_id in ("inpaint_remove_repair", "inpaint_replace_edit", "ipadapter_inpaint_reference")
    )
    mask_path = None
    mask_ref = None
    if inpaint_enabled:
        mask_path = out_dir / "inpaint_mask_alpha.png"
        create_mask(mask_path, source_size[0], source_size[1], shape=args.mask_shape)
        report["artifacts"]["mask"] = str(mask_path)
        mask_ref = client.upload_image(mask_path, overwrite=True)
        report["artifacts"]["uploaded_mask_ref"] = mask_ref
    if case_enabled(args, "inpaint_remove_repair"):
        cases.append({
            "id": "inpaint_remove_repair",
            "label": "inpaint delete and repair",
            "semantic_expectation": "Remove the masked object area and repair it into clean beach sand/ocean background.",
            "workflow": build_inpaint(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                mask_ref=mask_ref,
                prompt=prompts["inpaint_remove"],
                denoise=case_denoise(args, 0.86),
                prefix="hackme_i2i_inpaint_remove",
            ),
        })
    if case_enabled(args, "inpaint_replace_edit"):
        cases.append({
            "id": "inpaint_replace_edit",
            "label": "inpaint replacement edit",
            "semantic_expectation": "Replace the masked region with a blue beach umbrella and small seashells.",
            "workflow": build_inpaint(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                mask_ref=mask_ref,
                prompt=prompts["inpaint_replace"],
                denoise=case_denoise(args, 0.86),
                prefix="hackme_i2i_inpaint_replace",
            ),
        })
    outpaint_nodes = "ImagePadForOutpaint" in object_info and (
        not args.outpaint_preserve_source
        or all(name in object_info for name in ("SolidMask", "FeatherMask", "ImageCompositeMasked"))
    )
    if outpaint_nodes and case_enabled(args, "outpaint_expand_beach"):
        padding = outpaint_padding(args)
        cases.append({
            "id": "outpaint_expand_beach",
            "label": "outpainting",
            "semantic_expectation": "Extend the scene beyond the original image without neutral-gray padding or obvious borders.",
            "expect_larger_than_source": True,
            "outpaint_padding": padding,
            "outpaint_source_feather": int(args.outpaint_source_feather),
            "outpaint_preserve_source": bool(args.outpaint_preserve_source),
            "outpaint_method": selected_outpaint_method(args, object_info),
            "outpaint_seam_prefill": outpaint_seam_prefill_enabled(args, object_info),
            "expected_dimensions": (
                source_size[0] + padding["left"] + padding["right"],
                source_size[1] + padding["top"] + padding["bottom"],
            ),
            "workflow": build_outpaint(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                source_size=source_size,
                prompt=prompts["outpaint"],
                prefix="hackme_i2i_outpaint",
            ),
        })
    elif case_enabled(args, "outpaint_expand_beach"):
        missing = [
            name
            for name in (
                ("ImagePadForOutpaint", "SolidMask", "FeatherMask", "ImageCompositeMasked")
                if args.outpaint_preserve_source
                else ("ImagePadForOutpaint",)
            )
            if name not in object_info
        ]
        if args.only_case:
            raise ProbeError(f"outpaint requires seam-safe masking nodes: {', '.join(missing)}")
        report["skips"].append({
            "id": "outpaint_expand_beach",
            "reason": f"Seam-safe source preservation nodes are missing: {', '.join(missing)}",
        })
    if controlnet and case_enabled(args, f"controlnet_copy_composition_{safe_name(controlnet['type'])}"):
        control_case = dict(controlnet)
        control_case.update({"strength": args.control_strength, "start_percent": 0.0, "end_percent": 1.0})
        cases.append({
            "id": f"controlnet_copy_composition_{safe_name(control_case['type'])}",
            "label": f"ControlNet copy composition ({control_case['type']})",
            "semantic_expectation": "Use the source-derived control image to keep the lying pose/composition while changing outfit/details by prompt.",
            "workflow": build_img2img(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                prompt=prompts["controlnet"],
                denoise=case_denoise(args, 0.72),
                prefix="hackme_i2i_controlnet",
                controlnet=control_case,
            ),
        })
    elif not args.only_case:
        report["skips"].append({"id": "controlnet_copy_composition", "reason": "No compatible ControlNet loader/model/preprocessor combination was detected."})
    if "ImageScale" in object_info and case_enabled(args, "upscale_redraw_imagescale"):
        upscale_size = scaled_dimensions(*source_size, float(args.upscale_factor))
        cases.append({
            "id": "upscale_redraw_imagescale",
            "label": "upscale redraw",
            "semantic_expectation": "Scale up the source and run a low-denoise redraw to add detail while preserving the scene.",
            "expect_larger_than_source": True,
            "expected_dimensions": upscale_size,
            "workflow": build_upscale_redraw(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                source_size=source_size,
                prompt=prompts["upscale_redraw"],
                prefix="hackme_i2i_upscale_redraw",
            ),
        })
    elif not args.only_case:
        report["skips"].append({"id": "upscale_redraw", "reason": "ImageScale is missing, so redraw-upscale cannot be built without an upscaler model/template."})
    if ipadapter_style_compatible and blend_ref and case_enabled(args, "two_image_blend_mix"):
        cases.append({
            "id": "two_image_blend_mix",
            "label": "two-image semantic blend and redraw",
            "semantic_expectation": "Use the second image as a semantic/style reference while preserving one coherent source composition.",
            "notes": "Uses IPAdapter style/composition conditioning; direct pixel ImageBlend is intentionally forbidden because it produces ghosted double exposures.",
            "workflow": build_two_image_blend(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                blend_ref=blend_ref,
                prompt=prompts["blend"],
                prefix="hackme_i2i_blend",
            ),
        })
    elif not args.only_case:
        report["skips"].append({
            "id": "two_image_blend",
            "reason": "A compatible explicit SDXL IPAdapter/CLIP Vision pair was not available, the selected checkpoint is not explicitly SDXL, or no --blend-image-path was provided; direct pixel ImageBlend is not accepted as a semantic blend.",
        })
    if (
        ipadapter_style_compatible
        and style_ref
        and case_enabled(args, "ipadapter_style_reference")
    ):
        cases.append({
            "id": "ipadapter_style_reference",
            "label": "IPAdapter style/reference imitation",
            "semantic_expectation": "Use a separate style reference image while preserving the source image composition.",
            "notes": "Requires IPAdapter and CLIP Vision model files on the ComfyUI host.",
            "workflow": build_ipadapter_style_reference(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                style_ref=style_ref,
                prompt=prompts["style_reference"],
                prefix="hackme_i2i_ipadapter_style",
            ),
        })
    elif not args.only_case:
        report["skips"].append({
            "id": "ipadapter_style_reference",
            "reason": "A compatible explicit SDXL IPAdapter/CLIP Vision pair was not available, the selected checkpoint is not explicitly SDXL, or no --style-image-path was provided.",
        })
    if (
        ipadapter_style_compatible
        and style_ref
        and mask_ref
        and case_enabled(args, "ipadapter_inpaint_reference")
    ):
        cases.append({
            "id": "ipadapter_inpaint_reference",
            "label": "IPAdapter reference plus masked inpaint",
            "semantic_expectation": "Use a separate reference image while changing only the masked clothing region.",
            "notes": "Tests whether IPAdapter can be composed with the inpaint conditioning path for local reference-guided edits.",
            "workflow": build_ipadapter_inpaint_reference(
                args,
                object_info,
                model_name,
                source_ref=source_ref,
                mask_ref=mask_ref,
                style_ref=style_ref,
                prompt=prompts["ipadapter_inpaint"],
                denoise=case_denoise(args, float(args.ipadapter_denoise)),
                prefix="hackme_i2i_ipadapter_inpaint",
            ),
        })
    elif not args.only_case:
        report["skips"].append({
            "id": "ipadapter_inpaint_reference",
            "reason": "A compatible explicit SDXL IPAdapter/CLIP Vision pair, style image, or inpaint mask is missing.",
        })
    if args.only_case and not cases:
        raise ProbeError(f"--only-case did not match a runnable case: {args.only_case}")

    for case in cases:
        print(f"[i2i-matrix] running {case['id']}...", flush=True)
        result = run_case(client, args, case=case, workflow=case["workflow"], source_path=source_path, mask_path=mask_path)
        report["cases"].append(result)
        print(f"[i2i-matrix] {case['id']}: {result['status']} {result.get('output', {}).get('path', result.get('error', ''))}", flush=True)
        write_json(Path(args.out_json), report)

    failed = [case for case in report["cases"] if case.get("status") != "pass"]
    report["technical_ok"] = not failed
    report["semantic_review_required_cases"] = [
        case["id"]
        for case in report["cases"]
        if (case.get("semantic_verification") or {}).get("status") == "manual_review_required"
    ]
    report["semantic_ok"] = not report["semantic_review_required_cases"]
    report["ok"] = report["technical_ok"] and report["semantic_ok"]
    report["finished_at"] = now_iso()
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Run a standalone direct-ComfyUI img2img/inpaint/outpaint/controlnet matrix.")
    parser.add_argument("--interactive", action="store_true", help="Prompt for common options while keeping CLI defaults.")
    parser.add_argument("--comfyui-url", default="http://127.0.0.1:8188")
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--prompt", default=DEFAULT_SOURCE_PROMPT, help="Base positive prompt used by non-source cases unless a case overrides it.")
    parser.add_argument("--source-prompt", default=DEFAULT_SOURCE_PROMPT)
    parser.add_argument("--prompt-suite", choices=("beach_catgirl", "legacy_2girls"), default="beach_catgirl")
    parser.add_argument("--negative-prompt", default=DEFAULT_NEGATIVE_PROMPT)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--cfg", type=float, default=6.5)
    parser.add_argument("--seed", type=int, default=20260529)
    parser.add_argument("--sampler", default="euler")
    parser.add_argument("--scheduler", default="normal")
    parser.add_argument("--controlnet-type", default="canny")
    parser.add_argument("--controlnet-model", default="", help="Optional exact ControlNet model name/path from ControlNetLoader.")
    parser.add_argument("--control-strength", type=float, default=0.8)
    parser.add_argument("--outpaint", type=int, default=128)
    parser.add_argument("--outpaint-left", type=int, default=None)
    parser.add_argument("--outpaint-top", type=int, default=None)
    parser.add_argument("--outpaint-right", type=int, default=None)
    parser.add_argument("--outpaint-bottom", type=int, default=None)
    parser.add_argument("--outpaint-feathering", type=int, default=64)
    parser.add_argument("--outpaint-source-feather", type=int, default=128, help="Legacy source-composite feather pixels; used only when --outpaint-preserve-source is explicitly enabled.")
    parser.add_argument("--outpaint-seam-prefill", choices=("auto", "on", "off"), default="off", help="Experimental: use the installed pixel-space inpaint model to replace gray outpaint padding before latent sampling. Disabled by default until visually validated for the selected model.")
    parser.add_argument("--outpaint-prefill-model", default="MAT_Places512_G_fp16.safetensors", help="Installed pixel-space inpaint model used by --outpaint-seam-prefill.")
    parser.add_argument(
        "--outpaint-preserve-source",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Legacy compatibility option: composite the opaque source rectangle back after sampling. "
            "Disabled by default because studio/flat source backgrounds can create a visible rectangular seam; "
            "prefer the fully blended or semantic outpaint workflow."
        ),
    )
    parser.add_argument("--approve-semantic-review", action="store_true", help="Record operator approval for the visual semantics of all generated cases; without this, a technical run is not overall green.")
    parser.add_argument("--outpaint-denoise", type=float, default=1.0, help="Denoise used for the extended canvas; full redraw uses 1.0 so neutral padding cannot survive.")
    parser.add_argument("--inpaint-method", choices=("auto", "conditioning", "vae_encode"), default="auto")
    parser.add_argument("--outpaint-method", choices=("auto", "full_redraw", "conditioning", "vae_encode"), default="auto", help="Sampling path for outpaint. Auto uses a fully blended redraw so generic checkpoints replace gray padding; choose a masked method for a dedicated inpaint checkpoint.")
    parser.add_argument("--inpaint-noise-mask", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--differential-diffusion", action="store_true")
    parser.add_argument("--differential-strength", type=float, default=1.0)
    parser.add_argument("--upscale-factor", type=float, default=1.25)
    parser.add_argument("--upscale-denoise", type=float, default=0.28)
    parser.add_argument("--blend-image-path", default="", help="Optional second image for the two_image_blend_mix case.")
    parser.add_argument("--blend-factor", type=float, default=0.5)
    parser.add_argument("--blend-mode", default="normal")
    parser.add_argument("--blend-denoise", type=float, default=0.38)
    parser.add_argument("--style-image-path", default="", help="Optional style/reference image for the ipadapter_style_reference case.")
    parser.add_argument("--ipadapter-preset", default="PLUS (high strength)")
    parser.add_argument("--ipadapter-style-weight", type=float, default=0.85)
    parser.add_argument("--ipadapter-composition-weight", type=float, default=0.85)
    parser.add_argument("--ipadapter-denoise", type=float, default=0.45)
    parser.add_argument("--request-timeout", type=int, default=90)
    parser.add_argument("--max-seconds", type=int, default=1200)
    parser.add_argument("--poll-seconds", type=float, default=3)
    parser.add_argument("--out-dir", default="/tmp/hackme_comfyui_i2i_matrix")
    parser.add_argument("--out-json", default="")
    parser.add_argument("--source-image-path", default="", help="Use an existing source PNG/JPG instead of generating a reference source.")
    parser.add_argument("--only-case", default="", help="Run one case id, for step-by-step live audit.")
    parser.add_argument("--case-prompt", default="", help="Override the positive prompt for --only-case.")
    parser.add_argument("--case-denoise", type=float, default=0.0, help="Override denoise for supported single-case img2img probes.")
    parser.add_argument("--mask-shape", choices=("default", "window", "background_wall", "small_wall", "kimono_clothes"), default="default")
    parser.add_argument("--synthetic-source-only", action="store_true", help="Use a PIL fixture source instead of generating the reference source with txt2img.")
    parser.add_argument("--allow-synthetic-fallback", action="store_true", help="Fall back to a PIL fixture source if the reference txt2img source fails.")
    args = parser.parse_args(argv)
    args = apply_interactive_prompts(args)
    args = normalize_runtime_paths(args)
    if not args.out_json:
        args.out_json = str(Path(args.out_dir) / "i2i_matrix_report.json")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    report_path = Path(args.out_json).expanduser().resolve()
    report = {
        "ok": False,
        "label": "standalone_comfyui_i2i_matrix",
        "started_at": now_iso(),
        "artifacts": {"report": str(report_path), "out_dir": str(Path(args.out_dir).expanduser().resolve())},
    }
    try:
        report = run_matrix(args)
        return_code = 0 if report.get("ok") else 1
    except Exception as exc:
        report["ok"] = False
        report["error"] = sanitize_text(exc)
        report["traceback"] = sanitize_text(traceback.format_exc(limit=8))
        report["finished_at"] = now_iso()
        return_code = 1
    finally:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(report_path, report)
        print(json.dumps({
            "ok": report.get("ok"),
            "report": str(report_path),
            "out_dir": str(Path(args.out_dir).expanduser().resolve()),
            "passed": sum(1 for item in report.get("cases", []) if item.get("status") == "pass"),
            "failed": sum(1 for item in report.get("cases", []) if item.get("status") == "fail"),
            "technical_ok": report.get("technical_ok"),
            "semantic_ok": report.get("semantic_ok"),
            "semantic_review_required": report.get("semantic_review_required_cases", []),
            "skipped": len(report.get("skips", [])),
            "error": report.get("error"),
        }, ensure_ascii=False, indent=2))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
