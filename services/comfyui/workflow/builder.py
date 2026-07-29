"""Workflow builder helpers for ComfyUI generation modes."""

from services.comfyui.template.safety import next_safe_node_id


# The regular checkpoint outpaint shortcut cannot reliably blend an opaque
# studio/white backdrop into newly generated pixels.  Keep the model mapping
# identical to the checked-in ``origin_flux_fill_outpaint_gguf_q3`` official
# workflow, rather than creating a second, drifting implementation in the UI
# code path.
FLUX_FILL_OUTPAINT_GGUF_UNET = "flux1-fill-dev-Q3_K_S.gguf"
FLUX_FILL_OUTPAINT_CLIP_L = "clip_l.safetensors"
FLUX_FILL_OUTPAINT_T5 = "t5xxl_fp8_e4m3fn_scaled.safetensors"
FLUX_FILL_OUTPAINT_VAE = "ae.safetensors"
SAM3_OUTPAINT_SUBJECT_CHECKPOINT = "sam3.1_multiplex_fp16.safetensors"
DEFAULT_OUTPAINT_SUBJECT_PROMPT = "main subject"


def build_text_to_image_base(params):
    workflow = {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": params["model"]},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": params["prompt"], "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": params.get("negative_prompt") or "", "clip": ["4", 1]},
        },
    }
    final_model = ["4", 0]
    final_clip = ["4", 1]
    # Allocator (§7.4) returns max(used)+1 (which is 8 for the 4/6/7 base above).
    # Keep the historical floor of 10 so existing baseline / regression tests that
    # assert specific id placement (LoraLoader → "10", VAELoader → "11", etc.)
    # stay stable; the allocator still bumps above 10 if any caller pre-spliced
    # nodes at id ≥ 10 before invoking the builder helper.
    next_node_id = max(next_safe_node_id(workflow), 10)
    for item in params.get("loras") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        node_id = str(next_node_id)
        next_node_id += 1
        workflow[node_id] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": final_model,
                "clip": final_clip,
                "lora_name": name,
                "strength_model": float(item.get("strength_model", 1.0)),
                "strength_clip": float(item.get("strength_clip", 1.0)),
            },
        }
        final_model = [node_id, 0]
        final_clip = [node_id, 1]
    vae_ref = ["4", 2]
    vae_name = str(params.get("vae") or "").strip()
    if vae_name:
        vae_node_id = str(next_node_id)
        next_node_id += 1
        workflow[vae_node_id] = {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae_name},
        }
        vae_ref = [vae_node_id, 0]
    workflow["6"]["inputs"]["clip"] = final_clip
    workflow["7"]["inputs"]["clip"] = final_clip
    return workflow, final_model, final_clip, vae_ref, next_node_id


def attach_controlnet(workflow, params, *, positive_ref, negative_ref, next_node_id, error_cls):
    control = params.get("controlnet") if isinstance(params.get("controlnet"), dict) else None
    if not control:
        return positive_ref, negative_ref, next_node_id
    control_image = control.get("image_ref") if isinstance(control.get("image_ref"), dict) else None
    if not control_image or not control_image.get("filename"):
        raise error_cls("ControlNet 缺少控制圖")
    loader_id = str(next_node_id)
    next_node_id += 1
    workflow[loader_id] = {
        "class_type": "LoadImage",
        "inputs": {"image": control_image["filename"], "upload": "image"},
    }
    image_ref = [loader_id, 0]
    preprocessor = str(control.get("preprocessor") or "").strip()
    if preprocessor:
        preprocessor_id = str(next_node_id)
        next_node_id += 1
        workflow[preprocessor_id] = {
            "class_type": preprocessor,
            "inputs": {"image": image_ref},
        }
        image_ref = [preprocessor_id, 0]
    model_id = str(next_node_id)
    next_node_id += 1
    workflow[model_id] = {
        "class_type": "ControlNetLoader",
        "inputs": {"control_net_name": control["model_name"]},
    }
    apply_id = str(next_node_id)
    next_node_id += 1
    workflow[apply_id] = {
        "class_type": "ControlNetApplyAdvanced",
        "inputs": {
            "positive": positive_ref,
            "negative": negative_ref,
            "control_net": [model_id, 0],
            "image": image_ref,
            "strength": float(control.get("strength") or 1.0),
            "start_percent": float(control.get("start_percent") or 0.0),
            "end_percent": float(control.get("end_percent") or 1.0),
        },
    }
    return [apply_id, 0], [apply_id, 1], next_node_id


def build_text_to_image_workflow(params, *, error_cls):
    workflow, final_model, _final_clip, vae_ref, next_node_id = build_text_to_image_base(params)
    workflow["5"] = {
        "class_type": "EmptyLatentImage",
        "inputs": {
            "width": int(params["width"]),
            "height": int(params["height"]),
            "batch_size": int(params.get("batch_size") or 1),
        },
    }
    positive_ref = ["6", 0]
    negative_ref = ["7", 0]
    positive_ref, negative_ref, next_node_id = attach_controlnet(
        workflow,
        params,
        positive_ref=positive_ref,
        negative_ref=negative_ref,
        next_node_id=next_node_id,
        error_cls=error_cls,
    )
    workflow["3"] = {
        "class_type": "KSampler",
        "inputs": {
            "seed": int(params["seed"]),
            "steps": int(params["steps"]),
            "cfg": float(params["cfg"]),
            "sampler_name": params["sampler_name"],
            "scheduler": params["scheduler"],
            "denoise": 1,
            "model": final_model,
            "positive": positive_ref,
            "negative": negative_ref,
            "latent_image": ["5", 0],
        },
    }
    workflow["8"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": vae_ref},
    }
    workflow["9"] = {
        "class_type": "SaveImage",
        "inputs": {
            "filename_prefix": params.get("filename_prefix") or "hackme_web",
            "images": ["8", 0],
        },
    }
    return workflow


def build_gguf_text_to_image_base(params, *, error_cls):
    unet_name = str(params.get("comfyui_gguf_unet_name") or params.get("diffusion_model") or params.get("model") or "").strip()
    clip_loader_class = str(params.get("clip_loader_class") or "DualCLIPLoader").strip() or "DualCLIPLoader"
    clip_name1 = str(params.get("clip") or params.get("clip_name1") or "").strip()
    clip_name2 = str(params.get("clip2") or params.get("clip_name2") or "").strip()
    clip_name3 = str(params.get("clip3") or params.get("clip_name3") or "").strip()
    vae_name = str(params.get("vae") or "").strip()
    if not unet_name:
        raise error_cls("ComfyUI-GGUF workflow 缺少 UNet GGUF 模型")
    if not clip_name1 or not clip_name2:
        raise error_cls("ComfyUI-GGUF workflow 缺少必要文字編碼器")
    if clip_loader_class.startswith("TripleCLIPLoader") and not clip_name3:
        raise error_cls("ComfyUI-GGUF workflow 缺少第三組文字編碼器")
    if not vae_name:
        raise error_cls("ComfyUI-GGUF workflow 缺少 VAE；請選擇 profile 指定 VAE")

    clip_inputs = {
        "clip_name1": clip_name1,
        "clip_name2": clip_name2,
    }
    if clip_loader_class.startswith("TripleCLIPLoader"):
        clip_inputs["clip_name3"] = clip_name3
    elif clip_loader_class in {"DualCLIPLoader", "DualCLIPLoaderGGUF"}:
        clip_inputs["type"] = str(params.get("clip_type") or "sdxl").strip() or "sdxl"
        if clip_loader_class == "DualCLIPLoader":
            clip_inputs["device"] = str(params.get("clip_device") or "default").strip() or "default"

    workflow = {
        "4": {
            "class_type": "UnetLoaderGGUF",
            "inputs": {"unet_name": unet_name},
        },
        "10": {
            "class_type": clip_loader_class,
            "inputs": clip_inputs,
        },
        "11": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae_name},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": params["prompt"], "clip": ["10", 0]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": params.get("negative_prompt") or "", "clip": ["10", 0]},
        },
    }
    final_model = ["4", 0]
    final_clip = ["10", 0]
    next_node_id = 12
    for item in params.get("loras") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        node_id = str(next_node_id)
        next_node_id += 1
        workflow[node_id] = {
            "class_type": "LoraLoader",
            "inputs": {
                "model": final_model,
                "clip": final_clip,
                "lora_name": name,
                "strength_model": float(item.get("strength_model", 1.0)),
                "strength_clip": float(item.get("strength_clip", 1.0)),
            },
        }
        final_model = [node_id, 0]
        final_clip = [node_id, 1]
    workflow["6"]["inputs"]["clip"] = final_clip
    workflow["7"]["inputs"]["clip"] = final_clip
    return workflow, final_model, final_clip, ["11", 0], next_node_id


def build_gguf_text_to_image_workflow(params, *, error_cls):
    workflow, final_model, _final_clip, vae_ref, next_node_id = build_gguf_text_to_image_base(params, error_cls=error_cls)
    workflow["5"] = {
        "class_type": "EmptyLatentImage",
        "inputs": {
            "width": int(params["width"]),
            "height": int(params["height"]),
            "batch_size": int(params.get("batch_size") or 1),
        },
    }
    workflow["3"] = {
        "class_type": "KSampler",
        "inputs": {
            "seed": int(params["seed"]),
            "steps": int(params["steps"]),
            "cfg": float(params["cfg"]),
            "sampler_name": params["sampler_name"],
            "scheduler": params["scheduler"],
            "denoise": 1,
            "model": final_model,
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    }
    workflow["8"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": vae_ref},
    }
    workflow["9"] = {
        "class_type": "SaveImage",
        "inputs": {
            "filename_prefix": params.get("filename_prefix") or "hackme_web",
            "images": ["8", 0],
        },
    }
    return workflow


def build_sd3_gguf_text_to_image_workflow(params, *, error_cls):
    workflow, final_model, _final_clip, vae_ref, next_node_id = build_gguf_text_to_image_base(params, error_cls=error_cls)
    model_sampling_id = str(next_node_id)
    next_node_id += 1
    latent_id = str(next_node_id)
    next_node_id += 1
    zero_id = str(next_node_id)
    next_node_id += 1
    late_negative_id = str(next_node_id)
    next_node_id += 1
    early_negative_id = str(next_node_id)
    next_node_id += 1
    combined_negative_id = str(next_node_id)
    next_node_id += 1
    negative_split = float(params.get("sd3_negative_split") or 0.1)
    latent_width = int(params.get("sd3_native_width") or params.get("latent_width") or params["width"])
    latent_height = int(params.get("sd3_native_height") or params.get("latent_height") or params["height"])
    output_width = int(params.get("output_width") or params["width"])
    output_height = int(params.get("output_height") or params["height"])
    workflow[model_sampling_id] = {
        "class_type": "ModelSamplingSD3",
        "inputs": {
            "model": final_model,
            "shift": float(params.get("sd3_shift") or params.get("model_shift") or 3.0),
        },
    }
    workflow[latent_id] = {
        "class_type": "EmptySD3LatentImage",
        "inputs": {
            "width": latent_width,
            "height": latent_height,
            "batch_size": int(params.get("batch_size") or 1),
        },
    }
    workflow[zero_id] = {
        "class_type": "ConditioningZeroOut",
        "inputs": {"conditioning": ["7", 0]},
    }
    workflow[late_negative_id] = {
        "class_type": "ConditioningSetTimestepRange",
        "inputs": {
            "conditioning": [zero_id, 0],
            "start": negative_split,
            "end": 1.0,
        },
    }
    workflow[early_negative_id] = {
        "class_type": "ConditioningSetTimestepRange",
        "inputs": {
            "conditioning": ["7", 0],
            "start": 0.0,
            "end": negative_split,
        },
    }
    workflow[combined_negative_id] = {
        "class_type": "ConditioningCombine",
        "inputs": {
            "conditioning_1": [late_negative_id, 0],
            "conditioning_2": [early_negative_id, 0],
        },
    }
    workflow["3"] = {
        "class_type": "KSampler",
        "inputs": {
            "seed": int(params["seed"]),
            "steps": int(params["steps"]),
            "cfg": float(params["cfg"]),
            "sampler_name": params["sampler_name"],
            "scheduler": params["scheduler"],
            "denoise": 1,
            "model": [model_sampling_id, 0],
            "positive": ["6", 0],
            "negative": [combined_negative_id, 0],
            "latent_image": [latent_id, 0],
        },
    }
    workflow["8"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": vae_ref},
    }
    image_ref = ["8", 0]
    if output_width != latent_width or output_height != latent_height:
        scale_id = str(next_node_id)
        next_node_id += 1
        workflow[scale_id] = {
            "class_type": "ImageScale",
            "inputs": {
                "image": image_ref,
                "upscale_method": str(params.get("output_upscale_method") or "lanczos").strip() or "lanczos",
                "width": output_width,
                "height": output_height,
                "crop": "disabled",
            },
        }
        image_ref = [scale_id, 0]
    workflow["9"] = {
        "class_type": "SaveImage",
        "inputs": {
            "filename_prefix": params.get("filename_prefix") or "hackme_web",
            "images": image_ref,
        },
    }
    return workflow


def build_image_to_image_workflow(params, *, error_cls):
    workflow, final_model, _final_clip, vae_ref, next_node_id = build_text_to_image_base(params)
    source_image = params.get("source_image_ref") if isinstance(params.get("source_image_ref"), dict) else None
    if not source_image:
        raise error_cls("圖生圖缺少來源圖片")
    workflow["5"] = {
        "class_type": "LoadImage",
        "inputs": {"image": source_image["filename"], "upload": "image"},
    }
    workflow["10"] = {
        "class_type": "VAEEncode",
        "inputs": {"pixels": ["5", 0], "vae": vae_ref},
    }
    positive_ref = ["6", 0]
    negative_ref = ["7", 0]
    positive_ref, negative_ref, next_node_id = attach_controlnet(
        workflow,
        params,
        positive_ref=positive_ref,
        negative_ref=negative_ref,
        next_node_id=next_node_id,
        error_cls=error_cls,
    )
    workflow["3"] = {
        "class_type": "KSampler",
        "inputs": {
            "seed": int(params["seed"]),
            "steps": int(params["steps"]),
            "cfg": float(params["cfg"]),
            "sampler_name": params["sampler_name"],
            "scheduler": params["scheduler"],
            "denoise": float(params.get("denoise_strength") or 0.65),
            "model": final_model,
            "positive": positive_ref,
            "negative": negative_ref,
            "latent_image": ["10", 0],
        },
    }
    workflow["8"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": vae_ref},
    }
    workflow["9"] = {
        "class_type": "SaveImage",
        "inputs": {
            "filename_prefix": params.get("filename_prefix") or "hackme_web",
            "images": ["8", 0],
        },
    }
    return workflow


def build_inpaint_workflow(params, *, error_cls):
    workflow, final_model, _final_clip, vae_ref, next_node_id = build_text_to_image_base(params)
    source_image = params.get("source_image_ref") if isinstance(params.get("source_image_ref"), dict) else None
    mask_image = params.get("mask_image_ref") if isinstance(params.get("mask_image_ref"), dict) else None
    if not source_image or not mask_image:
        raise error_cls("局部重繪缺少來源圖片或遮罩")
    workflow["5"] = {
        "class_type": "LoadImage",
        "inputs": {"image": source_image["filename"], "upload": "image"},
    }
    workflow["11"] = {
        "class_type": "LoadImageMask",
        # ComfyUI's alpha mask output is inverted (1 - alpha).  The frontend
        # mask editor emits white-on-black masks where white means "repaint",
        # so use an RGB channel to preserve that user-facing semantics.
        "inputs": {"image": mask_image["filename"], "channel": "red"},
    }
    workflow["10"] = {
        "class_type": "VAEEncodeForInpaint",
        "inputs": {"pixels": ["5", 0], "mask": ["11", 0], "vae": vae_ref, "grow_mask_by": 6},
    }
    positive_ref = ["6", 0]
    negative_ref = ["7", 0]
    positive_ref, negative_ref, next_node_id = attach_controlnet(
        workflow,
        params,
        positive_ref=positive_ref,
        negative_ref=negative_ref,
        next_node_id=next_node_id,
        error_cls=error_cls,
    )
    workflow["3"] = {
        "class_type": "KSampler",
        "inputs": {
            "seed": int(params["seed"]),
            "steps": int(params["steps"]),
            "cfg": float(params["cfg"]),
            "sampler_name": params["sampler_name"],
            "scheduler": params["scheduler"],
            "denoise": float(params.get("denoise_strength") or 0.8),
            "model": final_model,
            "positive": positive_ref,
            "negative": negative_ref,
            "latent_image": ["10", 0],
        },
    }
    workflow["8"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": vae_ref},
    }
    workflow["9"] = {
        "class_type": "SaveImage",
        "inputs": {
            "filename_prefix": params.get("filename_prefix") or "hackme_web",
            "images": ["8", 0],
        },
    }
    return workflow


def build_flux_fill_gguf_outpaint_workflow(params, *, error_cls):
    """Build the product's source-preserving Flux Fill outpaint graph.

    This mirrors the official GGUF template: the source image and generated
    canvas meet through ``InpaintModelConditioning`` plus
    ``DifferentialDiffusion``.  In particular, do not paste the opaque source
    back over the decoded result with ``ImageCompositeMasked``: that operation
    recreates the very rectangular seam that outpaint is meant to remove.
    """
    source_image = params.get("source_image_ref") if isinstance(params.get("source_image_ref"), dict) else None
    if not source_image or not str(source_image.get("filename") or "").strip():
        raise error_cls("向外延展缺少來源圖片")
    if params.get("loras"):
        raise error_cls("Flux Fill 外延不支援目前的 Checkpoint LoRA 快捷選擇，請改用相容的官方 workflow")
    if params.get("controlnet"):
        raise error_cls("Flux Fill 外延不支援目前的 Checkpoint ControlNet 快捷選擇，請改用相容的官方 workflow")

    expand = params.get("outpaint") if isinstance(params.get("outpaint"), dict) else {}
    unet_name = str(params.get("outpaint_flux_unet_name") or FLUX_FILL_OUTPAINT_GGUF_UNET).strip()
    clip_l_name = str(params.get("outpaint_flux_clip_l") or FLUX_FILL_OUTPAINT_CLIP_L).strip()
    t5_name = str(params.get("outpaint_flux_t5") or FLUX_FILL_OUTPAINT_T5).strip()
    vae_name = str(params.get("outpaint_flux_vae") or FLUX_FILL_OUTPAINT_VAE).strip()
    if not all((unet_name, clip_l_name, t5_name, vae_name)):
        raise error_cls("Flux Fill 外延缺少必要的 UNet、CLIP 或 VAE")

    return {
        "17": {
            "class_type": "LoadImage",
            "inputs": {"image": source_image["filename"], "upload": "image"},
        },
        "23": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": params["prompt"], "clip": ["34", 0]},
        },
        "26": {
            "class_type": "FluxGuidance",
            "inputs": {"conditioning": ["23", 0], "guidance": 3.5},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": int(params["seed"]),
                "steps": int(params["steps"]),
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["39", 0],
                "positive": ["38", 0],
                "negative": ["38", 1],
                "latent_image": ["38", 2],
            },
        },
        "31": {
            "class_type": "UnetLoaderGGUF",
            "inputs": {"unet_name": unet_name},
        },
        "32": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae_name},
        },
        "34": {
            "class_type": "DualCLIPLoader",
            "inputs": {
                "clip_name1": clip_l_name,
                "clip_name2": t5_name,
                "type": "flux",
                "device": "default",
            },
        },
        "38": {
            "class_type": "InpaintModelConditioning",
            "inputs": {
                "positive": ["26", 0],
                "negative": ["46", 0],
                "vae": ["32", 0],
                "pixels": ["44", 0],
                "mask": ["44", 1],
                "noise_mask": False,
            },
        },
        "39": {
            "class_type": "DifferentialDiffusion",
            "inputs": {"model": ["31", 0]},
        },
        "44": {
            "class_type": "ImagePadForOutpaint",
            "inputs": {
                "image": ["17", 0],
                "left": int(expand.get("left") or 0),
                "top": int(expand.get("top") or 0),
                "right": int(expand.get("right") or 0),
                "bottom": int(expand.get("bottom") or 0),
                "feathering": int(expand.get("feathering") or 24),
            },
        },
        "46": {
            "class_type": "ConditioningZeroOut",
            "inputs": {"conditioning": ["23", 0]},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["32", 0]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": params.get("filename_prefix") or "hackme_web",
                "images": ["8", 0],
            },
        },
    }


def build_flux_fill_sam3_subject_outpaint_workflow(params, *, error_cls):
    """Build the two source artifacts for strict semantic outpaint.

    A source image can contain an opaque studio/background rectangle.  Keeping
    it in a ComfyUI ``ImageCompositeMasked`` node makes that rectangle (or its
    colour bleed) part of the final image.  This workflow therefore produces
    two *separate* artifacts instead:

    * Flux Fill redraws a source-independent, aligned blank canvas.
    * SAM3 emits the original source with a foreground-positive alpha channel.

    ``services.comfyui.semantic_composite`` validates and joins those artifacts
    after ComfyUI has completed.  Keeping the final alpha composite outside the
    diffusion graph gives the product one clear fail-closed boundary and avoids
    a model-generated seam around the original rectangular image.  Crucially,
    the original source pixels do not enter Flux's inpaint conditioning: a
    full-mask inpaint graph can otherwise still reconstruct/hallucinate the
    original person behind the protected SAM3 foreground.
    """
    source_image = params.get("source_image_ref") if isinstance(params.get("source_image_ref"), dict) else None
    if not source_image or not str(source_image.get("filename") or "").strip():
        raise error_cls("向外延展缺少來源圖片")
    if params.get("loras"):
        raise error_cls("Flux Fill 外延不支援目前的 Checkpoint LoRA 快捷選擇，請改用相容的官方 workflow")
    if params.get("controlnet"):
        raise error_cls("Flux Fill 外延不支援目前的 Checkpoint ControlNet 快捷選擇，請改用相容的官方 workflow")
    if int(params.get("batch_size") or 1) != 1:
        raise error_cls("SAM3 語意外延一次僅支援一張；請以多次執行取得多個候選結果")

    expand = params.get("outpaint") if isinstance(params.get("outpaint"), dict) else {}
    subject_prompt = str(params.get("outpaint_subject_prompt") or DEFAULT_OUTPAINT_SUBJECT_PROMPT).strip()
    if not subject_prompt:
        raise error_cls("外延保留主體描述不可為空")
    unet_name = str(params.get("outpaint_flux_unet_name") or FLUX_FILL_OUTPAINT_GGUF_UNET).strip()
    clip_l_name = str(params.get("outpaint_flux_clip_l") or FLUX_FILL_OUTPAINT_CLIP_L).strip()
    t5_name = str(params.get("outpaint_flux_t5") or FLUX_FILL_OUTPAINT_T5).strip()
    vae_name = str(params.get("outpaint_flux_vae") or FLUX_FILL_OUTPAINT_VAE).strip()
    sam3_checkpoint = str(params.get("outpaint_sam3_checkpoint") or SAM3_OUTPAINT_SUBJECT_CHECKPOINT).strip()
    if not all((unet_name, clip_l_name, t5_name, vae_name, sam3_checkpoint)):
        raise error_cls("Flux Fill 前景外延缺少必要的 UNet、CLIP、VAE 或 SAM3 模型")

    canvas_width = int(params.get("outpaint_canvas_width") or 0)
    canvas_height = int(params.get("outpaint_canvas_height") or 0)
    if canvas_width < 64 or canvas_height < 64 or canvas_width > 16384 or canvas_height > 16384:
        raise error_cls("SAM3 語意外延缺少已驗證的對齊畫布尺寸")
    return {
        "17": {
            "class_type": "LoadImage",
            "inputs": {"image": source_image["filename"], "upload": "image"},
        },
        "23": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": params["prompt"], "clip": ["34", 0]},
        },
        "26": {
            "class_type": "FluxGuidance",
            "inputs": {"conditioning": ["23", 0], "guidance": 3.5},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": int(params["seed"]),
                "steps": int(params["steps"]),
                "cfg": 1.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["39", 0],
                "positive": ["38", 0],
                "negative": ["38", 1],
                "latent_image": ["38", 2],
            },
        },
        "31": {
            "class_type": "UnetLoaderGGUF",
            "inputs": {"unet_name": unet_name},
        },
        "32": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae_name},
        },
        "34": {
            "class_type": "DualCLIPLoader",
            "inputs": {
                "clip_name1": clip_l_name,
                "clip_name2": t5_name,
                "type": "flux",
                "device": "default",
            },
        },
        "38": {
            "class_type": "InpaintModelConditioning",
            "inputs": {
                "positive": ["26", 0],
                "negative": ["46", 0],
                "vae": ["32", 0],
                # Never feed the original source pixels to Flux.  Even a
                # full inpaint mask can retain that latent's person semantics
                # and create a second subject behind SAM3's protected one.
                "pixels": ["118", 0],
                "mask": ["120", 0],
                "noise_mask": False,
            },
        },
        "39": {
            "class_type": "DifferentialDiffusion",
            "inputs": {"model": ["31", 0]},
        },
        "118": {
            "class_type": "EmptyImage",
            "inputs": {
                "width": canvas_width,
                "height": canvas_height,
                "batch_size": 1,
                "color": 0,
            },
        },
        "114": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": subject_prompt, "clip": ["116", 1]},
        },
        "115": {
            "class_type": "SAM3_Detect",
            "inputs": {
                "model": ["116", 0],
                "image": ["17", 0],
                "conditioning": ["114", 0],
                "threshold": float(params.get("outpaint_subject_threshold") or 0.25),
                "refine_iterations": 2,
                "individual_masks": False,
            },
        },
        "116": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": sam3_checkpoint},
        },
        "117": {
            "class_type": "InvertMask",
            "inputs": {"mask": ["115", 0]},
        },
        # The canvas is an all-black `EmptyImage`; this full opaque mask tells
        # Flux Fill to generate every pixel from the prompt, without seeing an
        # original-image latent.  The source is used only by SAM3 below.
        "120": {
            "class_type": "SolidMask",
            "inputs": {"value": 1.0, "width": canvas_width, "height": canvas_height},
        },
        "46": {
            "class_type": "ConditioningZeroOut",
            "inputs": {"conditioning": ["23", 0]},
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["32", 0]},
        },
        "121": {
            "class_type": "JoinImageWithAlpha",
            "inputs": {
                "image": ["17", 0],
                # SAM3's detected mask is background-positive for this model.
                # Node 117 makes the alpha foreground-positive before the app
                # layer performs its final composite.
                "alpha": ["117", 0],
            },
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": params.get("filename_prefix") or "hackme_web",
                "images": ["8", 0],
            },
            "_meta": {"title": "Semantic outpaint background"},
        },
        "124": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": f"{params.get('filename_prefix') or 'hackme_web'}_semantic_foreground",
                "images": ["121", 0],
            },
            "_meta": {"title": "SAM3 semantic foreground"},
        },
    }


def build_outpaint_workflow(params, *, error_cls):
    family = str(params.get("outpaint_workflow_family") or "").strip().lower()
    if family == "flux_fill_sam3_subject_gguf":
        return build_flux_fill_sam3_subject_outpaint_workflow(params, error_cls=error_cls)
    if family == "flux_fill_gguf":
        return build_flux_fill_gguf_outpaint_workflow(params, error_cls=error_cls)
    workflow, final_model, _final_clip, vae_ref, next_node_id = build_text_to_image_base(params)
    source_image = params.get("source_image_ref") if isinstance(params.get("source_image_ref"), dict) else None
    if not source_image:
        raise error_cls("向外延展缺少來源圖片")
    expand = params.get("outpaint") if isinstance(params.get("outpaint"), dict) else {}
    workflow["5"] = {
        "class_type": "LoadImage",
        "inputs": {"image": source_image["filename"], "upload": "image"},
    }
    workflow["10"] = {
        "class_type": "ImagePadForOutpaint",
        "inputs": {
            "image": ["5", 0],
            "left": int(expand.get("left") or 0),
            "top": int(expand.get("top") or 0),
            "right": int(expand.get("right") or 0),
            "bottom": int(expand.get("bottom") or 0),
            "feathering": int(expand.get("feathering") or 24),
        },
    }
    workflow["11"] = {
        "class_type": "VAEEncodeForInpaint",
        "inputs": {"pixels": ["10", 0], "mask": ["10", 1], "vae": vae_ref, "grow_mask_by": 6},
    }
    positive_ref = ["6", 0]
    negative_ref = ["7", 0]
    positive_ref, negative_ref, next_node_id = attach_controlnet(
        workflow,
        params,
        positive_ref=positive_ref,
        negative_ref=negative_ref,
        next_node_id=next_node_id,
        error_cls=error_cls,
    )
    workflow["3"] = {
        "class_type": "KSampler",
        "inputs": {
            "seed": int(params["seed"]),
            "steps": int(params["steps"]),
            "cfg": float(params["cfg"]),
            "sampler_name": params["sampler_name"],
            "scheduler": params["scheduler"],
            "denoise": float(params.get("denoise_strength") or 0.9),
            "model": final_model,
            "positive": positive_ref,
            "negative": negative_ref,
            "latent_image": ["11", 0],
        },
    }
    workflow["8"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["3", 0], "vae": vae_ref},
    }
    workflow["9"] = {
        "class_type": "SaveImage",
        "inputs": {
            "filename_prefix": params.get("filename_prefix") or "hackme_web",
            "images": ["8", 0],
        },
    }
    return workflow


def build_upscale_workflow(params, *, error_cls):
    source_image = params.get("source_image_ref") if isinstance(params.get("source_image_ref"), dict) else None
    upscale_model = str(params.get("upscale_model") or "").strip()
    if not source_image:
        raise error_cls("放大修復缺少來源圖片")
    if not upscale_model:
        raise error_cls("請選擇放大模型")
    return {
        "3": {
            "class_type": "UpscaleModelLoader",
            "inputs": {"model_name": upscale_model},
        },
        "4": {
            "class_type": "LoadImage",
            "inputs": {"image": source_image["filename"], "upload": "image"},
        },
        "5": {
            "class_type": "ImageUpscaleWithModel",
            "inputs": {"upscale_model": ["3", 0], "image": ["4", 0]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {
                "filename_prefix": params.get("filename_prefix") or "hackme_web",
                "images": ["5", 0],
            },
        },
    }


def build_generation_workflow(params, *, error_cls):
    mode = str(params.get("generation_mode") or "txt2img").strip().lower()
    if params.get("comfyui_gguf_unet_name"):
        if mode != "txt2img":
            raise error_cls("ComfyUI-GGUF 快捷 workflow 目前只支援文字生圖；其他模式請使用 workflow 模板。")
        if str(params.get("workflow_family") or "").strip() == "sd3_triple_clip_gguf":
            return build_sd3_gguf_text_to_image_workflow(params, error_cls=error_cls)
        return build_gguf_text_to_image_workflow(params, error_cls=error_cls)
    if mode == "txt2img":
        return build_text_to_image_workflow(params, error_cls=error_cls)
    if mode == "img2img":
        return build_image_to_image_workflow(params, error_cls=error_cls)
    if mode == "inpaint":
        return build_inpaint_workflow(params, error_cls=error_cls)
    if mode == "outpaint":
        return build_outpaint_workflow(params, error_cls=error_cls)
    if mode == "upscale":
        return build_upscale_workflow(params, error_cls=error_cls)
    if mode in {"t2v", "i2v", "v2v", "t2s", "t2sv"}:
        raise error_cls("這個 ComfyUI 模式需要透過支援的大模型 workflow 模板執行，請先匯入或選擇對應 workflow。")
    raise error_cls("ComfyUI 產圖模式不支援")
