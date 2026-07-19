"""Workflow summary, dependency inference, and manifest contract helpers."""

import re

from services.comfyui.constants import CONTROLNET_TYPE_DEFINITIONS
from services.comfyui.validation.rules import WORKFLOW_BLOCKED_CLASS_RE, WorkflowValidationError


CONTROLNET_TYPE_ALIASES = {
    "canny": "canny",
    "depth": "depth",
    "openpose": "openpose",
    "pose": "openpose",
    "lineart": "lineart",
    "scribble": "scribble",
    "softedge": "softedge",
    "soft_edge": "softedge",
    "tile": "tile",
}

EMBEDDING_TAG_RE = re.compile(r"<\s*embeddings?\s*:\s*([^<>]+?)\s*>", re.IGNORECASE)
EMBEDDING_PREFIX_RE = re.compile(r"(?<![\w/])embedding:", re.IGNORECASE)
EMBEDDING_PREFIX_STOP_RE = re.compile(r"[,;<>\r\n]")
EMBEDDING_FILE_EXT_RE = re.compile(r"\.(?:safetensors|pt|pth|bin)\b", re.IGNORECASE)


# This map is deliberately exact.  A new loader or model-bearing loader input
# must be classified here before an official manifest can pass the dependency
# contract; guessing from a filename would let LoRA/ControlNet files leak into
# ``required_models`` again.
GRAPH_LOADER_DEPENDENCY_INPUTS = {
    ("CheckpointLoaderSimple", "ckpt_name"): ("models", "checkpoint"),
    ("UNETLoader", "unet_name"): ("models", "diffusion_model"),
    ("UnetLoaderGGUF", "unet_name"): ("models", "diffusion_model"),
    ("UnetLoaderGGUFAdvanced", "unet_name"): ("models", "diffusion_model"),
    ("VAELoader", "vae_name"): ("models", "vae"),
    ("CLIPLoader", "clip_name"): ("models", "clip"),
    ("CLIPLoaderGGUF", "clip_name"): ("models", "clip"),
    ("DualCLIPLoader", "clip_name1"): ("models", "clip"),
    ("DualCLIPLoader", "clip_name2"): ("models", "clip"),
    ("DualCLIPLoaderGGUF", "clip_name1"): ("models", "clip"),
    ("DualCLIPLoaderGGUF", "clip_name2"): ("models", "clip"),
    ("TripleCLIPLoaderGGUF", "clip_name1"): ("models", "clip"),
    ("TripleCLIPLoaderGGUF", "clip_name2"): ("models", "clip"),
    ("TripleCLIPLoaderGGUF", "clip_name3"): ("models", "clip"),
    ("CLIPVisionLoader", "clip_name"): ("models", "clip_vision"),
    ("UpscaleModelLoader", "model_name"): ("models", "upscale"),
    ("LatentUpscaleModelLoader", "model_name"): ("models", "latent_upscale"),
    ("LTXAVTextEncoderLoader", "ckpt_name"): ("models", "checkpoint"),
    ("LTXAVTextEncoderLoader", "text_encoder"): ("models", "clip"),
    ("LTXVAudioVAELoader", "ckpt_name"): ("models", "checkpoint"),
    ("LoraLoader", "lora_name"): ("loras", ""),
    ("LoraLoaderModelOnly", "lora_name"): ("loras", ""),
    ("ControlNetLoader", "control_net_name"): ("controlnets", ""),
}

EXPLICIT_CUSTOM_NODE_PACKAGES = {
    "UnetLoaderGGUF": "ComfyUI-GGUF",
    "UnetLoaderGGUFAdvanced": "ComfyUI-GGUF",
    "CLIPLoaderGGUF": "ComfyUI-GGUF",
    "DualCLIPLoaderGGUF": "ComfyUI-GGUF",
    "TripleCLIPLoaderGGUF": "ComfyUI-GGUF",
}

_MODEL_LIKE_LOADER_INPUTS = frozenset(
    input_name for _class_type, input_name in GRAPH_LOADER_DEPENDENCY_INPUTS
)
_GENERIC_MODEL_LIKE_LOADER_INPUTS = frozenset(
    {
        "model",
        "checkpoint",
        "ckpt",
        "lora",
        "controlnet",
        "control_net",
        "clip",
        "vae",
        "unet",
        "encoder",
        "text_encoder",
    }
)
_MODEL_KINDS = frozenset(
    kind
    for category, kind in GRAPH_LOADER_DEPENDENCY_INPUTS.values()
    if category == "models"
) | {"embedding"}


def clean_embedding_name(name):
    return str(name or "").strip().strip(".,;")


def extract_prefixed_embedding_names(text):
    source = str(text or "")
    matches = list(EMBEDDING_PREFIX_RE.finditer(source))
    names = []
    for index, match in enumerate(matches):
        start = match.end()
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(source)
        stop = EMBEDDING_PREFIX_STOP_RE.search(source, start, next_start)
        end = stop.start() if stop else next_start
        chunk = source[start:end].strip()
        if not chunk:
            continue
        ext_match = EMBEDDING_FILE_EXT_RE.search(chunk)
        if ext_match:
            name = chunk[:ext_match.end()]
        elif "/" in chunk or "\\" in chunk:
            name = chunk
        else:
            name = chunk.split(maxsplit=1)[0]
        name = clean_embedding_name(name)
        if name:
            names.append(name)
    return names


def extract_embedding_names_from_text(text):
    names = []
    seen = set()
    tag_names = [clean_embedding_name(match.group(1)) for match in EMBEDDING_TAG_RE.finditer(str(text or ""))]
    for name in tag_names + extract_prefixed_embedding_names(text):
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def infer_controlnet_type_from_name(name):
    normalized = str(name or "").strip().lower().replace("-", "_")
    if not normalized:
        return ""
    for token, control_type in CONTROLNET_TYPE_ALIASES.items():
        if token in normalized:
            return control_type
    for control_type in CONTROLNET_TYPE_DEFINITIONS:
        if control_type in normalized:
            return control_type
    return ""


def extract_workflow_summary(workflow_json):
    if not isinstance(workflow_json, dict):
        raise WorkflowValidationError("workflow JSON 必須是物件")
    required_models = []
    required_loras = []
    required_controlnets = []
    model_seen = set()
    lora_seen = set()
    control_seen = set()
    text_nodes = []
    generation_mode = "txt2img"
    default_params = {
        "generation_mode": "txt2img",
        "model": "",
        "diffusion_model": "",
        "clip": "",
        "vae": "",
        "prompt": "",
        "negative_prompt": "",
        "width": 0,
        "height": 0,
        "steps": 0,
        "cfg": 0,
        "seed": 0,
        "batch_size": 1,
        "sampler_name": "",
        "scheduler": "",
        "denoise_strength": 0,
        "upscale_model": "",
        "loras": [],
        "controlnet": None,
    }

    def add_model(kind, name):
        text = str(name or "").strip()
        if not text:
            return
        key = (kind, text)
        if key in model_seen:
            return
        model_seen.add(key)
        required_models.append({"kind": kind, "name": text})

    def add_lora(name, *, strength_model=None, strength_clip=None):
        text = str(name or "").strip()
        if not text or text in lora_seen:
            return
        lora_seen.add(text)
        entry = {"name": text}
        if strength_model is not None:
            entry["strength_model"] = strength_model
        if strength_clip is not None:
            entry["strength_clip"] = strength_clip
        required_loras.append(entry)
        default_params["loras"].append({
            "name": text,
            "strength_model": strength_model if strength_model is not None else 1,
            "strength_clip": strength_clip if strength_clip is not None else 1,
        })

    def add_controlnet(name, *, control_type="", preprocessor=""):
        text = str(name or "").strip()
        if not text:
            return
        key = (text, control_type or "", preprocessor or "")
        if key in control_seen:
            return
        control_seen.add(key)
        entry = {"name": text}
        if control_type:
            entry["type"] = control_type
        if preprocessor:
            entry["preprocessor"] = preprocessor
        required_controlnets.append(entry)
        if default_params["controlnet"] is None:
            default_params["controlnet"] = {
                "type": control_type or infer_controlnet_type_from_name(text),
                "model_name": text,
                "preprocessor": preprocessor or "",
                "strength": 1,
                "start_percent": 0,
                "end_percent": 1,
            }

    for node_id, node in workflow_json.items():
        if not isinstance(node, dict):
            raise WorkflowValidationError(f"workflow node {node_id} 格式不正確")
        class_type = str(node.get("class_type") or "").strip()
        if not class_type:
            raise WorkflowValidationError(f"workflow node {node_id} 缺少 class_type")
        if WORKFLOW_BLOCKED_CLASS_RE.search(class_type):
            raise WorkflowValidationError(f"workflow node {node_id} 使用了不允許的節點：{class_type}")
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            raise WorkflowValidationError(f"workflow node {node_id} 缺少 inputs")
        lower_class = class_type.lower()

        ckpt_name = inputs.get("ckpt_name")
        if isinstance(ckpt_name, str) and ckpt_name.strip():
            add_model("checkpoint", ckpt_name)
            if not default_params["model"]:
                default_params["model"] = ckpt_name.strip()

        vae_name = inputs.get("vae_name")
        if isinstance(vae_name, str) and vae_name.strip():
            add_model("vae", vae_name)
            if not default_params["vae"]:
                default_params["vae"] = vae_name.strip()

        unet_name = inputs.get("unet_name")
        if isinstance(unet_name, str) and unet_name.strip():
            add_model("diffusion_model", unet_name)
            if not default_params.get("diffusion_model"):
                default_params["diffusion_model"] = unet_name.strip()

        if lower_class == "clipvisionloader":
            clip_vision_name = inputs.get("clip_name")
            if isinstance(clip_vision_name, str) and clip_vision_name.strip():
                add_model("clip_vision", clip_vision_name)
        for clip_input_name in ("clip_name", "clip_name1", "clip_name2", "clip_name3"):
            if lower_class == "clipvisionloader" and clip_input_name == "clip_name":
                continue
            clip_name = inputs.get(clip_input_name)
            if isinstance(clip_name, str) and clip_name.strip():
                add_model("clip", clip_name)
                if not default_params["clip"]:
                    default_params["clip"] = clip_name.strip()

        if lower_class == "ltxavtextencoderloader":
            text_encoder = inputs.get("text_encoder")
            if isinstance(text_encoder, str) and text_encoder.strip():
                add_model("clip", text_encoder)
                if not default_params["clip"]:
                    default_params["clip"] = text_encoder.strip()

        if "loraloader" in lower_class:
            add_lora(
                inputs.get("lora_name"),
                strength_model=inputs.get("strength_model"),
                strength_clip=inputs.get("strength_clip"),
            )

        if "controlnetloader" in lower_class:
            add_controlnet(
                inputs.get("control_net_name") or inputs.get("model_name"),
                control_type=infer_controlnet_type_from_name(inputs.get("control_net_name") or inputs.get("model_name")),
            )

        if lower_class == "latentupscalemodelloader":
            add_model("latent_upscale", inputs.get("model_name"))
        elif lower_class == "upscalemodelloader":
            add_model("upscale", inputs.get("model_name"))
            if not default_params["upscale_model"] and isinstance(inputs.get("model_name"), str):
                default_params["upscale_model"] = inputs.get("model_name").strip()

        if "ksampler" in lower_class:
            if isinstance(inputs.get("seed"), (int, float)):
                default_params["seed"] = int(inputs.get("seed"))
            if isinstance(inputs.get("steps"), (int, float)):
                default_params["steps"] = int(inputs.get("steps"))
            if isinstance(inputs.get("cfg"), (int, float)):
                default_params["cfg"] = float(inputs.get("cfg"))
            if isinstance(inputs.get("sampler_name"), str):
                default_params["sampler_name"] = inputs.get("sampler_name").strip()
            if isinstance(inputs.get("scheduler"), str):
                default_params["scheduler"] = inputs.get("scheduler").strip()
            if isinstance(inputs.get("denoise"), (int, float)):
                default_params["denoise_strength"] = float(inputs.get("denoise"))

        if isinstance(inputs.get("text"), str):
            text = inputs.get("text").strip()
            if lower_class in {"cliptextencode", "cliptextencodeflux"}:
                text_nodes.append(text)
            for embedding_name in extract_embedding_names_from_text(text):
                add_model("embedding", embedding_name)

        if lower_class == "emptylatentimage":
            if isinstance(inputs.get("width"), (int, float)):
                default_params["width"] = int(inputs.get("width"))
            if isinstance(inputs.get("height"), (int, float)):
                default_params["height"] = int(inputs.get("height"))
            if isinstance(inputs.get("batch_size"), (int, float)):
                default_params["batch_size"] = max(1, int(inputs.get("batch_size")))

        if lower_class == "loadimagemask":
            generation_mode = "inpaint"
        elif lower_class == "imagepadforoutpaint":
            generation_mode = "outpaint"
        elif lower_class == "imageupscalewithmodel":
            generation_mode = "upscale"
        elif lower_class == "loadimage" and generation_mode == "txt2img":
            generation_mode = "img2img"

        if isinstance(inputs.get("preprocessor"), str) and default_params["controlnet"]:
            default_params["controlnet"]["preprocessor"] = inputs.get("preprocessor").strip()

    default_params["generation_mode"] = generation_mode
    if generation_mode == "upscale":
        default_params["prompt"] = ""
        default_params["negative_prompt"] = ""
    elif text_nodes:
        default_params["prompt"] = text_nodes[0]
        default_params["negative_prompt"] = text_nodes[1] if len(text_nodes) > 1 else ""
    if default_params["controlnet"] and not default_params["controlnet"].get("type"):
        default_params["controlnet"]["type"] = infer_controlnet_type_from_name(default_params["controlnet"].get("model_name"))

    return {
        "required_models": required_models,
        "required_loras": required_loras,
        "required_controlnets": required_controlnets,
        "default_params": default_params,
        "node_count": len(workflow_json),
    }


def _normalize_dependency_name(value):
    text = str(value or "").strip().replace("\\", "/")
    if not text:
        raise WorkflowValidationError("dependency name must not be empty")
    parts = text.split("/")
    if (
        text.startswith("/")
        or re.match(r"^[A-Za-z]:", text)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise WorkflowValidationError(f"dependency name is not a safe relative path: {value!r}")
    return text


def _looks_like_loader_dependency_input(input_name):
    key = str(input_name or "").strip().lower()
    if key in _MODEL_LIKE_LOADER_INPUTS or key in _GENERIC_MODEL_LIKE_LOADER_INPUTS:
        return True
    return key.endswith("_name") and any(
        token in key
        for token in ("model", "checkpoint", "ckpt", "lora", "control", "clip", "vae", "unet", "encoder")
    )


def _parse_manifest_dependency_list(manifest, field_name, *, category):
    raw_items = manifest.get(field_name)
    errors = []
    values = []
    seen = set()
    if not isinstance(raw_items, list):
        return [], [f"manifest.{field_name} must be a list"]
    for index, item in enumerate(raw_items):
        if category == "custom_nodes":
            if not isinstance(item, str) or not item.strip():
                errors.append(f"manifest.{field_name}[{index}] must be a non-empty package id")
                continue
            value = item.strip()
        else:
            if not isinstance(item, dict):
                errors.append(f"manifest.{field_name}[{index}] must be an object")
                continue
            try:
                name = _normalize_dependency_name(item.get("name"))
            except WorkflowValidationError as exc:
                errors.append(f"manifest.{field_name}[{index}]: {exc}")
                continue
            if category == "models":
                kind = str(item.get("kind") or "").strip()
                if kind not in _MODEL_KINDS:
                    errors.append(f"manifest.{field_name}[{index}] has unsupported model kind {kind!r}")
                    continue
                value = (kind, name)
            else:
                value = name
        if value in seen:
            errors.append(f"manifest.{field_name} contains duplicate dependency {value!r}")
            continue
        seen.add(value)
        values.append(value)
    return values, errors


def extract_graph_dependency_contract(workflow_json):
    """Return exact, categorized dependencies from a ComfyUI API graph.

    Model-bearing loader inputs are fail-closed: a literal dependency-looking
    input on a loader must have an explicit class/input mapping above.  Prompt
    embeddings are included as ``required_models(kind=embedding)`` even though
    they are not loader nodes.

    Custom-node package validation is intentionally narrower.  ComfyUI does
    not expose authoritative package provenance in an API graph, so only the
    explicit class-to-package map above participates in equality.  Potential
    non-core classes without such provenance remain visible in the machine
    result rather than being silently described as fully validated.
    """
    if not isinstance(workflow_json, dict) or not workflow_json:
        raise WorkflowValidationError("workflow JSON must be a non-empty object")

    # Import lazily: validation.sanitize imports this summary module while the
    # template package imports validation.sanitize during package startup.
    from services.comfyui.template.allowlist import (
        CONTROLNET_PREPROCESSOR_ALLOWLIST,
        ORIGIN_WORKFLOW_ALLOWLIST,
        is_allowed_class,
    )

    models = set()
    loras = set()
    controlnets = set()
    custom_nodes = set()
    mapped_custom_classes = set()
    class_types = set()
    errors = []

    for node_id, node in workflow_json.items():
        if not isinstance(node, dict):
            raise WorkflowValidationError(f"workflow node {node_id} must be an object")
        class_type = str(node.get("class_type") or "").strip()
        inputs = node.get("inputs")
        if not class_type or not isinstance(inputs, dict):
            raise WorkflowValidationError(f"workflow node {node_id} must have class_type and object inputs")
        class_types.add(class_type)

        package_id = EXPLICIT_CUSTOM_NODE_PACKAGES.get(class_type)
        if package_id:
            custom_nodes.add(package_id)
            mapped_custom_classes.add(class_type)

        for input_name, raw_value in inputs.items():
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            mapping = GRAPH_LOADER_DEPENDENCY_INPUTS.get((class_type, str(input_name)))
            if mapping:
                try:
                    normalized_name = _normalize_dependency_name(raw_value)
                except WorkflowValidationError as exc:
                    errors.append(f"node {node_id} {class_type}.{input_name}: {exc}")
                    continue
                category, kind = mapping
                if category == "models":
                    models.add((kind, normalized_name))
                elif category == "loras":
                    loras.add(normalized_name)
                elif category == "controlnets":
                    controlnets.add(normalized_name)
                continue
            if "loader" in class_type.lower() and _looks_like_loader_dependency_input(input_name):
                errors.append(
                    f"unmapped loader dependency input: node {node_id} {class_type}.{input_name}"
                )

            if input_name == "text":
                for embedding_name in extract_embedding_names_from_text(raw_value):
                    try:
                        models.add(("embedding", _normalize_dependency_name(embedding_name)))
                    except WorkflowValidationError as exc:
                        errors.append(f"node {node_id} embedding: {exc}")

    model_names = {name for _kind, name in models}
    overlaps = {
        "models_loras": sorted(model_names & loras),
        "models_controlnets": sorted(model_names & controlnets),
        "loras_controlnets": sorted(loras & controlnets),
    }
    for label, names in overlaps.items():
        if names:
            errors.append(f"graph dependency category overlap {label}: {names}")

    explicit_package_classes = set(EXPLICIT_CUSTOM_NODE_PACKAGES)
    unknown_classes = sorted(class_type for class_type in class_types if not is_allowed_class(class_type))
    if unknown_classes:
        errors.append(f"graph contains classes without allowlist/package authority: {unknown_classes}")
    provenance_candidates = set(ORIGIN_WORKFLOW_ALLOWLIST) | set(CONTROLNET_PREPROCESSOR_ALLOWLIST)
    unmapped_package_provenance = sorted(
        (class_types & provenance_candidates) - explicit_package_classes
    )

    return {
        "models": sorted(models),
        "loras": sorted(loras),
        "controlnets": sorted(controlnets),
        "custom_nodes": sorted(custom_nodes),
        "errors": errors,
        "category_overlaps": overlaps,
        "custom_node_evidence": {
            "scope": "explicit_class_to_package_mapping_only",
            "mapped_graph_classes": sorted(mapped_custom_classes),
            "mapped_packages": sorted(custom_nodes),
            "unmapped_package_provenance_classes": unmapped_package_provenance,
            "unknown_graph_classes": unknown_classes,
            "limitation": (
                "API graphs do not provide authoritative package provenance; "
                "unmapped allowlisted non-core classes are disclosed but are not inferred as packages"
            ),
        },
    }


def validate_manifest_dependency_contract(workflow_json, manifest):
    """Compare graph dependencies to a manifest with strict category equality."""
    if not isinstance(manifest, dict):
        raise WorkflowValidationError("manifest must be an object")
    graph = extract_graph_dependency_contract(workflow_json)
    manifest_models, model_errors = _parse_manifest_dependency_list(
        manifest, "required_models", category="models"
    )
    manifest_loras, lora_errors = _parse_manifest_dependency_list(
        manifest, "required_loras", category="loras"
    )
    manifest_controlnets, controlnet_errors = _parse_manifest_dependency_list(
        manifest, "required_controlnets", category="controlnets"
    )
    manifest_custom_nodes, custom_errors = _parse_manifest_dependency_list(
        manifest, "required_custom_nodes", category="custom_nodes"
    )

    manifest_sets = {
        "models": set(manifest_models),
        "loras": set(manifest_loras),
        "controlnets": set(manifest_controlnets),
        "custom_nodes": set(manifest_custom_nodes),
    }
    graph_sets = {
        "models": set(map(tuple, graph["models"])),
        "loras": set(graph["loras"]),
        "controlnets": set(graph["controlnets"]),
        "custom_nodes": set(graph["custom_nodes"]),
    }
    manifest_model_names = {name for _kind, name in manifest_sets["models"]}
    manifest_overlaps = {
        "models_loras": sorted(manifest_model_names & manifest_sets["loras"]),
        "models_controlnets": sorted(manifest_model_names & manifest_sets["controlnets"]),
        "loras_controlnets": sorted(manifest_sets["loras"] & manifest_sets["controlnets"]),
    }
    errors = list(graph["errors"]) + model_errors + lora_errors + controlnet_errors + custom_errors
    for label, names in manifest_overlaps.items():
        if names:
            errors.append(f"manifest dependency category overlap {label}: {names}")

    differences = {}
    for category in ("models", "loras", "controlnets", "custom_nodes"):
        missing = sorted(graph_sets[category] - manifest_sets[category])
        extra = sorted(manifest_sets[category] - graph_sets[category])
        differences[category] = {"missing_from_manifest": missing, "extra_in_manifest": extra}
        if missing or extra:
            errors.append(f"{category} dependency mismatch: missing={missing} extra={extra}")

    return {
        "schema_version": "hackme.comfyui-manifest-dependency-contract/v1",
        "ok": not errors,
        "scope": {
            "model_dependencies": "exact_loader_class_and_input_mapping_plus_prompt_embeddings",
            "custom_nodes": "explicit_class_to_package_mapping_only",
        },
        "graph": {
            "models": [{"kind": kind, "name": name} for kind, name in graph["models"]],
            "loras": list(graph["loras"]),
            "controlnets": list(graph["controlnets"]),
            "custom_nodes": list(graph["custom_nodes"]),
        },
        "manifest": {
            "models": [{"kind": kind, "name": name} for kind, name in sorted(manifest_sets["models"])],
            "loras": sorted(manifest_sets["loras"]),
            "controlnets": sorted(manifest_sets["controlnets"]),
            "custom_nodes": sorted(manifest_sets["custom_nodes"]),
        },
        "differences": differences,
        "manifest_category_overlaps": manifest_overlaps,
        "custom_node_evidence": graph["custom_node_evidence"],
        "errors": errors,
    }
