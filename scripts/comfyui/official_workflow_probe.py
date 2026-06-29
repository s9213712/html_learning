#!/usr/bin/env python3
"""Probe checked-in official ComfyUI workflows against a live ComfyUI API.

This intentionally bypasses hackme_web routes. It validates whether the
official workflow bundles themselves can be accepted by the target ComfyUI
server, then optionally queues each runnable workflow and fetches its output.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import struct
import sys
import time
import zlib
from io import BytesIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.comfyui.client import ComfyUIClient, ComfyUIError  # noqa: E402
from services.comfyui.template.capability import resolve_model_option  # noqa: E402
from services.comfyui.template.seeding import SYSTEM_WORKFLOW_IDS  # noqa: E402


MODEL_INPUTS = {
    "CheckpointLoaderSimple": {"ckpt_name": ("CheckpointLoaderSimple", "ckpt_name")},
    "UNETLoader": {"unet_name": ("UNETLoader", "unet_name")},
    "UnetLoaderGGUF": {"unet_name": ("UnetLoaderGGUF", "unet_name")},
    "UnetLoaderGGUFAdvanced": {"unet_name": ("UnetLoaderGGUFAdvanced", "unet_name")},
    "CLIPLoader": {"clip_name": ("CLIPLoader", "clip_name")},
    "CLIPLoaderGGUF": {"clip_name": ("CLIPLoaderGGUF", "clip_name")},
    "DualCLIPLoader": {
        "clip_name1": ("DualCLIPLoader", "clip_name1"),
        "clip_name2": ("DualCLIPLoader", "clip_name2"),
    },
    "DualCLIPLoaderGGUF": {
        "clip_name1": ("DualCLIPLoaderGGUF", "clip_name1"),
        "clip_name2": ("DualCLIPLoaderGGUF", "clip_name2"),
    },
    "TripleCLIPLoaderGGUF": {
        "clip_name1": ("TripleCLIPLoaderGGUF", "clip_name1"),
        "clip_name2": ("TripleCLIPLoaderGGUF", "clip_name2"),
        "clip_name3": ("TripleCLIPLoaderGGUF", "clip_name3"),
    },
    "VAELoader": {"vae_name": ("VAELoader", "vae_name")},
    "LoraLoader": {"lora_name": ("LoraLoader", "lora_name")},
    "LoraLoaderModelOnly": {"lora_name": ("LoraLoaderModelOnly", "lora_name")},
    "ControlNetLoader": {"control_net_name": ("ControlNetLoader", "control_net_name")},
    "UpscaleModelLoader": {"model_name": ("UpscaleModelLoader", "model_name")},
    "LatentUpscaleModelLoader": {"model_name": ("LatentUpscaleModelLoader", "model_name")},
    "LoadVideo": {"file": ("LoadVideo", "file")},
}

HEAVY_WORKFLOWS = {
    "origin_audio_ace_step_15_xl_base",
    "origin_capybara_video_edit",
    "origin_wan_vace_inpainting",
    "origin_wan22_14b_i2v_subgraphed",
    "origin_ltx23_t2v",
}

SMOKE_DEFAULTS = {
    "steps": 2,
    "width": 512,
    "height": 512,
    "prompt": "hackme_web official workflow probe",
    "negative_prompt": "low quality, blurry, watermark, child, minor",
}

MINOR_PROMPT_RE = re.compile(
    r"\b(?:child|children|kid|kids|minor|underage|toddler|infant|baby|loli|lolita|schoolgirl|young\s+girl|girl)\b"
    r"|(?:小女孩|幼女|未成年|蘿莉|萝莉)",
    re.IGNORECASE,
)
AGE_UNDER_18_RE = re.compile(r"\b(?:[0-9]|1[0-7])\s*(?:-| )?\s*(?:year[- ]?old|yo)\b", re.IGNORECASE)
SEXUALIZED_PROMPT_RE = re.compile(
    r"\b(?:underwear|panties|bra|nude|naked|breast|breasts|nipple|nipples|vagina|vaginal|pubic|sex|sexual|erotic|nsfw|"
    r"without\s+(?:a\s+)?bra|without\s+underwear|no\s+underwear|spread\s+legs|bed)\b"
    r"|(?:內衣|内衣|裸體|裸体|胸部|乳頭|乳头|陰部|阴部|性化|沒穿內褲|没穿内裤|床上)",
    re.IGNORECASE,
)


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _png_rgba(width, height, rgba):
    r, g, b, a = [max(0, min(255, int(v))) for v in rgba]
    row = bytes([r, g, b, a]) * int(width)
    raw = b"".join(b"\x00" + row for _ in range(int(height)))

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack("!I", len(payload)) + body + struct.pack("!I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", int(width), int(height), 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, level=9))
        + chunk(b"IEND", b"")
    )


def _image_quality_issue(data):
    if not isinstance(data, (bytes, bytearray)) or not data:
        return ""
    try:
        from PIL import Image, ImageStat

        with Image.open(BytesIO(bytes(data))) as image:
            sample = image.convert("RGB")
            sample.thumbnail((256, 256))
            stat = ImageStat.Stat(sample)
    except Exception as exc:
        return f"image cannot be decoded: {exc}"
    extrema = stat.extrema or []
    means = stat.mean or []
    if not extrema or not means:
        return "image statistics are empty"
    max_channel = max((pair[1] for pair in extrema), default=0)
    min_channel = min((pair[0] for pair in extrema), default=0)
    dynamic_range = max_channel - min_channel
    mean_value = sum(float(value or 0) for value in means) / max(len(means), 1)
    if max_channel <= 2 and mean_value <= 1.5:
        return "image is almost entirely black"
    if min_channel >= 253 and mean_value >= 253:
        return "image is almost entirely white"
    if dynamic_range <= 2:
        return "image is almost a single solid color"
    return ""


def _output_quality_issues(output):
    images = output.get("images") if isinstance(output.get("images"), list) else []
    if not images and output.get("data"):
        images = [output]
    issues = []
    for index, item in enumerate(images, start=1):
        if not isinstance(item, dict):
            continue
        issue = _image_quality_issue(item.get("data") or b"")
        if issue:
            image_ref = item.get("image_ref") if isinstance(item.get("image_ref"), dict) else {}
            filename = image_ref.get("filename") or f"image #{index}"
            issues.append(f"{filename}: {issue}")
    return issues


def _load_workflow(bundle_id):
    path = REPO_ROOT / "workflows" / "comfyui" / bundle_id / "workflow.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _node_options(object_info, node_class, input_name):
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


def _literal_model_name(value):
    if not isinstance(value, str):
        return ""
    return value.strip()


def _preflight(bundle_id, workflow, object_info):
    available_classes = set(object_info.keys()) if isinstance(object_info, dict) else set()
    missing_nodes = []
    missing_models = []
    for node_id, node in sorted((workflow or {}).items(), key=lambda item: str(item[0])):
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "").strip()
        if not class_type:
            missing_nodes.append({"node_id": node_id, "class_type": ""})
            continue
        if class_type not in available_classes:
            missing_nodes.append({"node_id": node_id, "class_type": class_type})
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        for input_name, option_ref in (MODEL_INPUTS.get(class_type) or {}).items():
            value = _literal_model_name(inputs.get(input_name))
            if not value:
                continue
            options = _node_options(object_info, *option_ref)
            if options is not None and not resolve_model_option(value, options):
                missing_models.append({
                    "node_id": node_id,
                    "class_type": class_type,
                    "input": input_name,
                    "value": value,
                })
    return {
        "bundle_id": bundle_id,
        "missing_nodes": missing_nodes,
        "missing_models": missing_models,
        "runnable": not missing_nodes and not missing_models,
    }


def _prompt_safety_issue(workflow):
    unsafe_nodes = []
    for node_id, node in sorted((workflow or {}).items(), key=lambda item: str(item[0])):
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        for input_name in ("text", "prompt"):
            value = inputs.get(input_name)
            if not isinstance(value, str):
                continue
            if not SEXUALIZED_PROMPT_RE.search(value):
                continue
            if MINOR_PROMPT_RE.search(value) or AGE_UNDER_18_RE.search(value):
                unsafe_nodes.append(f"{node_id}.{input_name}")
    if not unsafe_nodes:
        return ""
    return (
        "blocked unsafe prompt before queueing: sexualized minor or age-ambiguous childlike content "
        f"in node input(s) {', '.join(unsafe_nodes[:8])}"
    )


def _load_json_object(value, *, label):
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{label} must be a JSON object")
    return payload


def _load_custom_params(args):
    params = {}
    if getattr(args, "custom_param_file", ""):
        path = Path(str(args.custom_param_file)).expanduser()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("--custom-param-file must contain a JSON object") from exc
        if not isinstance(payload, dict):
            raise ValueError("--custom-param-file must contain a JSON object")
        params.update(payload)
    params.update(_load_json_object(getattr(args, "custom_param_json", ""), label="--custom-param-json"))
    if getattr(args, "custom_params", False):
        aliases = {
            "prompt": getattr(args, "prompt", None),
            "negative_prompt": getattr(args, "negative_prompt", None),
            "steps": getattr(args, "steps", None),
            "width": getattr(args, "width", None),
            "height": getattr(args, "height", None),
            "checkpoint_model": getattr(args, "checkpoint_model", None),
        }
        for key, value in aliases.items():
            if value is not None and value != "":
                params[key] = value
    direct = {
        "prompt": getattr(args, "custom_prompt", None),
        "negative_prompt": getattr(args, "custom_negative_prompt", None),
        "seed": getattr(args, "custom_seed", None),
        "steps": getattr(args, "custom_steps", None),
        "width": getattr(args, "custom_width", None),
        "height": getattr(args, "custom_height", None),
        "cfg": getattr(args, "custom_cfg", None),
        "sampler_name": getattr(args, "custom_sampler_name", None),
        "scheduler": getattr(args, "custom_scheduler", None),
        "batch_size": getattr(args, "custom_batch_size", None),
        "checkpoint_model": getattr(args, "custom_checkpoint_model", None),
        "diffusion_model": getattr(args, "custom_diffusion_model", None),
        "clip_model": getattr(args, "custom_clip_model", None),
        "vae_model": getattr(args, "custom_vae_model", None),
        "lora_model": getattr(args, "custom_lora_model", None),
        "lora_strength_model": getattr(args, "custom_lora_strength_model", None),
        "lora_strength_clip": getattr(args, "custom_lora_strength_clip", None),
        "controlnet_model": getattr(args, "custom_controlnet_model", None),
        "upscale_model": getattr(args, "custom_upscale_model", None),
    }
    for key, value in direct.items():
        if value is not None and value != "":
            params[key] = value
    return params


def _conditioning_source_node_ids(workflow, node_ref, *, input_names=None, seen=None):
    if seen is None:
        seen = set()
    if not isinstance(node_ref, list) or not node_ref:
        return set()
    node_id = str(node_ref[0])
    if not node_id or node_id in seen:
        return set()
    seen.add(node_id)
    node = workflow.get(node_id) if isinstance(workflow, dict) else None
    if not isinstance(node, dict):
        return {node_id}
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    class_type = str(node.get("class_type") or "")
    source_ids = {node_id}
    names = input_names or ("conditioning",)
    if class_type == "ReferenceLatent":
        names = ("conditioning",)
    for input_name in names:
        upstream_ref = inputs.get(input_name)
        if isinstance(upstream_ref, list) and upstream_ref:
            source_ids.update(_conditioning_source_node_ids(workflow, upstream_ref, seen=seen))
    return source_ids


def _negative_conditioning_node_ids(workflow):
    negative_node_ids = set()
    if not isinstance(workflow, dict):
        return negative_node_ids
    for node in workflow.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        ref = inputs.get("negative")
        if isinstance(ref, list) and ref:
            negative_node_ids.update(_conditioning_source_node_ids(workflow, ref))
    return negative_node_ids


def _apply_generation_params_to_node(node_id, node, params, negative_node_ids):
    if not isinstance(node, dict) or not isinstance(params, dict):
        return
    class_type = str(node.get("class_type") or "")
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    if "checkpoint_model" in params and class_type == "CheckpointLoaderSimple" and "ckpt_name" in inputs:
        inputs["ckpt_name"] = params["checkpoint_model"]
    if "diffusion_model" in params and class_type in {"UNETLoader", "UnetLoaderGGUF", "UnetLoaderGGUFAdvanced"} and "unet_name" in inputs:
        inputs["unet_name"] = params["diffusion_model"]
    if "clip_model" in params and class_type in {"CLIPLoader", "CLIPLoaderGGUF"} and "clip_name" in inputs:
        inputs["clip_name"] = params["clip_model"]
    if "clip_model" in params and class_type in {"DualCLIPLoader", "TripleCLIPLoader", "DualCLIPLoaderGGUF", "TripleCLIPLoaderGGUF"}:
        for input_name in ("clip_name1", "clip_name2", "clip_name3"):
            if input_name in inputs:
                inputs[input_name] = params["clip_model"]
    if "vae_model" in params and class_type == "VAELoader" and "vae_name" in inputs:
        inputs["vae_name"] = params["vae_model"]
    if "lora_model" in params and class_type in {"LoraLoader", "LoraLoaderModelOnly"} and "lora_name" in inputs:
        inputs["lora_name"] = params["lora_model"]
    if class_type in {"LoraLoader", "LoraLoaderModelOnly"}:
        if "lora_strength_model" in params and "strength_model" in inputs:
            inputs["strength_model"] = params["lora_strength_model"]
        if "lora_strength_clip" in params and "strength_clip" in inputs:
            inputs["strength_clip"] = params["lora_strength_clip"]
    if "controlnet_model" in params and class_type == "ControlNetLoader" and "control_net_name" in inputs:
        inputs["control_net_name"] = params["controlnet_model"]
    if "upscale_model" in params and class_type == "UpscaleModelLoader" and "model_name" in inputs:
        inputs["model_name"] = params["upscale_model"]
    for input_name in ("seed", "steps", "width", "height", "cfg", "sampler_name", "scheduler", "batch_size"):
        if input_name in params and input_name in inputs:
            inputs[input_name] = params[input_name]
    if "batch_size" in params:
        for input_name in ("max_images", "number_of_images"):
            if input_name in inputs:
                inputs[input_name] = params["batch_size"]
    if "width" in params and "height" in params and "size_preset" in inputs and isinstance(inputs.get("size_preset"), str):
        inputs["size_preset"] = f"{int(params['width'])}x{int(params['height'])} (1:1)"
    if "prompt" in params and "prompt" in inputs and isinstance(inputs.get("prompt"), str):
        inputs["prompt"] = params.get("negative_prompt", inputs["prompt"]) if str(node_id) in negative_node_ids else params["prompt"]
    if "negative_prompt" in params and "prompt" in inputs and isinstance(inputs.get("prompt"), str) and str(node_id) in negative_node_ids:
        inputs["prompt"] = params["negative_prompt"]
    if "prompt" in params and "text" in inputs and isinstance(inputs.get("text"), str):
        inputs["text"] = params.get("negative_prompt", inputs["text"]) if str(node_id) in negative_node_ids else params["prompt"]
    if "negative_prompt" in params and "text" in inputs and isinstance(inputs.get("text"), str) and str(node_id) in negative_node_ids:
        inputs["text"] = params["negative_prompt"]


def _apply_explicit_node_overrides(patched, params):
    class_inputs = params.get("class_inputs") if isinstance(params.get("class_inputs"), dict) else {}
    for node in patched.values():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        overrides = class_inputs.get(class_type)
        if isinstance(overrides, dict):
            inputs = node.setdefault("inputs", {})
            if isinstance(inputs, dict):
                inputs.update(overrides)
    node_inputs = params.get("node_inputs") if isinstance(params.get("node_inputs"), dict) else {}
    for node_id, overrides in node_inputs.items():
        node = patched.get(str(node_id))
        if isinstance(node, dict) and isinstance(overrides, dict):
            inputs = node.setdefault("inputs", {})
            if isinstance(inputs, dict):
                inputs.update(overrides)


def _patch_for_probe(
    workflow,
    bundle_id,
    *,
    width,
    height,
    steps,
    prompt,
    negative_prompt,
    checkpoint_model,
    source_image_name,
    mask_image_name,
    parameter_mode,
    custom_params=None,
):
    patched = copy.deepcopy(workflow)
    custom_params = dict(custom_params or {})
    if parameter_mode == "smoke":
        generation_params = {
            "steps": steps if steps is not None else SMOKE_DEFAULTS["steps"],
            "width": width if width is not None else SMOKE_DEFAULTS["width"],
            "height": height if height is not None else SMOKE_DEFAULTS["height"],
            "prompt": prompt if prompt is not None else SMOKE_DEFAULTS["prompt"],
            "negative_prompt": negative_prompt if negative_prompt is not None else SMOKE_DEFAULTS["negative_prompt"],
        }
        if checkpoint_model:
            generation_params["checkpoint_model"] = checkpoint_model
    elif parameter_mode == "custom":
        generation_params = custom_params
    else:
        generation_params = {}
    negative_node_ids = _negative_conditioning_node_ids(patched)
    for node_id, node in patched.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get("class_type") or "")
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        _apply_generation_params_to_node(node_id, node, generation_params, negative_node_ids)
        if "filename_prefix" in inputs:
            output_kind = "probe"
            if "audio" in class_type.lower():
                output_kind = "audio"
            elif "video" in class_type.lower():
                output_kind = "video"
            inputs["filename_prefix"] = f"{output_kind}/hackme_official_probe/{bundle_id}"
        if class_type == "LoadImage":
            inputs["image"] = source_image_name
            inputs["upload"] = "image"
        elif class_type == "LoadImageMask":
            inputs["image"] = mask_image_name
            inputs["upload"] = "image"
    if parameter_mode == "custom":
        _apply_explicit_node_overrides(patched, custom_params)
    return patched


def _result(bundle_id, *, status, detail="", preflight=None, elapsed_ms=None, output=None):
    payload = {
        "bundle_id": bundle_id,
        "status": status,
        "detail": detail,
        "checked_at": _now(),
    }
    if preflight is not None:
        payload["preflight"] = preflight
    if elapsed_ms is not None:
        payload["elapsed_ms"] = int(elapsed_ms)
    if output is not None:
        payload["output"] = output
    return payload


def _queue_prompt_location(client, prompt_id, *, timeout_seconds=None):
    prompt_id = str(prompt_id or "").strip()
    if not prompt_id:
        return "unknown"
    try:
        queue = client._json_request("/queue", timeout=timeout_seconds)
    except Exception as exc:
        return f"unknown: queue read failed: {exc}"
    for key, label in (("queue_running", "running"), ("queue_pending", "pending")):
        items = queue.get(key) if isinstance(queue, dict) else []
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) > 1 and str(item[1]) == prompt_id:
                return label
    return "absent"


def run_probe(args):
    client = ComfyUIClient(args.comfyui_url, timeout=args.request_timeout)
    object_info = client.get_object_info()
    source_ref = client.upload_image_bytes(
        _png_rgba(args.image_size, args.image_size, (80, 140, 230, 255)),
        "hackme_official_probe_source.png",
        overwrite=True,
    )
    mask_ref = client.upload_image_bytes(
        _png_rgba(args.image_size, args.image_size, (255, 255, 255, 255)),
        "hackme_official_probe_mask.png",
        overwrite=True,
    )
    source_image_name = source_ref["filename"]
    mask_image_name = mask_ref["filename"]

    bundle_ids = list(SYSTEM_WORKFLOW_IDS)
    if args.only:
        wanted = {item.strip() for item in args.only.split(",") if item.strip()}
        bundle_ids = [item for item in bundle_ids if item in wanted]
    custom_params = _load_custom_params(args)
    parameter_mode = "custom" if args.custom_params or custom_params else ("formal" if args.formal_params else "smoke")
    results = []
    for bundle_id in bundle_ids:
        workflow = _load_workflow(bundle_id)
        preflight = _preflight(bundle_id, workflow, object_info)
        if not preflight["runnable"]:
            if args.preflight_only or not args.force_run:
                results.append(_result(bundle_id, status="preflight_failed", preflight=preflight))
                if not args.continue_on_fail:
                    break
                continue
        if args.preflight_only:
            results.append(_result(bundle_id, status="preflight_pass", preflight=preflight))
            continue
        if bundle_id in HEAVY_WORKFLOWS and not args.include_heavy:
            results.append(_result(bundle_id, status="skipped_heavy", preflight=preflight, detail="Use --include-heavy to run this heavy audio/video workflow."))
            continue
        patched = _patch_for_probe(
            workflow,
            bundle_id,
            width=args.width,
            height=args.height,
            steps=args.steps,
            prompt=args.prompt,
            negative_prompt=args.negative_prompt,
            checkpoint_model=args.checkpoint_model,
            source_image_name=source_image_name,
            mask_image_name=mask_image_name,
            parameter_mode=parameter_mode,
            custom_params=custom_params,
        )
        safety_issue = _prompt_safety_issue(patched)
        if safety_issue:
            results.append(_result(bundle_id, status="blocked_unsafe_prompt", detail=safety_issue, preflight=preflight))
            if not args.continue_on_fail:
                break
            continue
        start = time.perf_counter()
        try:
            if args.acceptance_only:
                prompt_id = client.queue_prompt(patched)
                elapsed_ms = (time.perf_counter() - start) * 1000
                cleanup_details = []
                try:
                    client.delete_queue_items([prompt_id], timeout_seconds=args.request_timeout)
                except Exception as exc:
                    cleanup_details.append(f"queue delete failed: {exc}")
                location = _queue_prompt_location(client, prompt_id, timeout_seconds=args.request_timeout)
                if location == "running":
                    try:
                        client.interrupt(timeout_seconds=args.request_timeout)
                    except Exception as exc:
                        cleanup_details.append(f"interrupt failed: {exc}")
                    location = _queue_prompt_location(client, prompt_id, timeout_seconds=args.request_timeout)
                if location != "absent":
                    cleanup_details.append(f"prompt cleanup location: {location}")
                detail = "accepted only; output intentionally skipped by --acceptance-only"
                if cleanup_details:
                    detail = f"{detail}; {'; '.join(cleanup_details)}"
                results.append(_result(
                    bundle_id,
                    status="accepted",
                    detail=detail,
                    preflight=preflight,
                    elapsed_ms=elapsed_ms,
                    output={"prompt_id": prompt_id},
                ))
                continue
            output = client.generate_from_workflow(
                patched,
                timeout_seconds=args.timeout,
                expected_count=1,
                fetch_outputs=not args.no_fetch_outputs,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            media = output.get("media") if isinstance(output.get("media"), dict) else {}
            output_summary = {
                "prompt_id": output.get("prompt_id"),
                "primary_ref": output.get("image_ref"),
                "mime_type": output.get("mime_type"),
                "bytes": len(output.get("data") or b""),
                "image_count": len(output.get("images") or []),
                "video_count": len(media.get("videos") or []),
                "audio_count": len(media.get("audio") or []),
            }
            quality_issues = [] if args.no_fetch_outputs else _output_quality_issues(output)
            if quality_issues:
                output_summary["quality_issues"] = quality_issues[:5]
                results.append(_result(
                    bundle_id,
                    status="run_failed",
                    detail="output quality check failed: " + "; ".join(quality_issues[:5]),
                    preflight=preflight,
                    elapsed_ms=elapsed_ms,
                    output=output_summary,
                ))
                if not args.continue_on_fail:
                    break
                continue
            results.append(_result(bundle_id, status="completed", preflight=preflight, elapsed_ms=elapsed_ms, output=output_summary))
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            results.append(_result(bundle_id, status="run_failed", preflight=preflight, elapsed_ms=elapsed_ms, detail=str(exc)))
            if not args.continue_on_fail:
                break

    completed = sum(1 for item in results if item["status"] in {"completed", "accepted"})
    failed = sum(1 for item in results if item["status"] in {"preflight_failed", "run_failed", "blocked_unsafe_prompt"})
    return {
        "ok": failed == 0,
        "summary": {
            "comfyui_url": args.comfyui_url,
            "started_at": _now(),
            "bundle_count": len(bundle_ids),
            "completed": completed,
            "failed": failed,
            "preflight_only": bool(args.preflight_only),
            "include_heavy": bool(args.include_heavy),
            "formal_params": bool(args.formal_params),
            "force_run": bool(args.force_run),
            "parameter_mode": parameter_mode,
            "custom_param_keys": sorted(key for key in custom_params.keys() if key not in {"node_inputs", "class_inputs"}),
            "fetch_outputs": not bool(args.no_fetch_outputs),
            "acceptance_only": bool(args.acceptance_only),
        },
        "results": results,
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Probe official hackme_web ComfyUI workflows.")
    parser.add_argument("--comfyui-url", default="http://127.0.0.1:8188")
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--request-timeout", type=int, default=30)
    parser.add_argument("--steps", type=int, default=None, help="Smoke/custom override for node inputs named steps.")
    parser.add_argument("--width", type=int, default=None, help="Smoke/custom override for node inputs named width.")
    parser.add_argument("--height", type=int, default=None, help="Smoke/custom override for node inputs named height.")
    parser.add_argument("--image-size", type=int, default=128)
    parser.add_argument("--prompt", default=None, help="Smoke/custom override for prompt text inputs.")
    parser.add_argument("--negative-prompt", default=None, help="Smoke/custom override for negative prompt text inputs.")
    parser.add_argument("--checkpoint-model", default="", help="Smoke/custom override for CheckpointLoaderSimple ckpt_name.")
    parser.add_argument("--only", default="", help="Comma-separated workflow ids to test.")
    parser.add_argument("--include-heavy", action="store_true", help="Also run audio/video heavy workflows.")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--continue-on-fail", action="store_true")
    parser.add_argument("--force-run", action="store_true", help="Queue workflows even when preflight reports missing nodes or models.")
    parser.add_argument("--formal-params", action="store_true", help="Keep workflow generation parameters exactly as checked in; only remap probe input files/output prefixes.")
    parser.add_argument("--custom-params", action="store_true", help="Start from the checked-in workflow and apply only explicitly supplied custom parameter overrides.")
    parser.add_argument("--custom-param-json", default="", help="JSON object of custom parameter overrides.")
    parser.add_argument("--custom-param-file", default="", help="Path to a JSON object of custom parameter overrides.")
    parser.add_argument("--custom-prompt", default=None)
    parser.add_argument("--custom-negative-prompt", default=None)
    parser.add_argument("--custom-seed", type=int, default=None)
    parser.add_argument("--custom-steps", type=int, default=None)
    parser.add_argument("--custom-width", type=int, default=None)
    parser.add_argument("--custom-height", type=int, default=None)
    parser.add_argument("--custom-cfg", type=float, default=None)
    parser.add_argument("--custom-sampler-name", default=None)
    parser.add_argument("--custom-scheduler", default=None)
    parser.add_argument("--custom-batch-size", type=int, default=None)
    parser.add_argument("--custom-checkpoint-model", default=None)
    parser.add_argument("--custom-diffusion-model", default=None)
    parser.add_argument("--custom-clip-model", default=None)
    parser.add_argument("--custom-vae-model", default=None)
    parser.add_argument("--custom-lora-model", default=None)
    parser.add_argument("--custom-lora-strength-model", type=float, default=None)
    parser.add_argument("--custom-lora-strength-clip", type=float, default=None)
    parser.add_argument("--custom-controlnet-model", default=None)
    parser.add_argument("--custom-upscale-model", default=None)
    parser.add_argument("--no-fetch-outputs", action="store_true", help="Validate execution and output references without downloading generated media bytes.")
    parser.add_argument("--acceptance-only", action="store_true", help="Queue each prompt to validate ComfyUI accepts it, then interrupt instead of waiting for generated outputs.")
    parser.add_argument("--json-out", default="")
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        report = run_probe(args)
    except Exception as exc:
        report = {
            "ok": False,
            "summary": {"comfyui_url": args.comfyui_url, "started_at": _now(), "bundle_count": 0, "completed": 0, "failed": 1},
            "results": [_result("probe", status="probe_failed", detail=str(exc))],
        }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if report.get("ok") else 1)


if __name__ == "__main__":
    main()
