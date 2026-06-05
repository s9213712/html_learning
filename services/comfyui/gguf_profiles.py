"""Official GGUF model profiles for ComfyUI routing.

GGUF image models are not interchangeable single-file checkpoints.  Each
customer-facing option must be mapped to the companion text encoders, VAE,
loader class, and verified workflow family before it can be routed safely.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import PurePosixPath


_WAI_VARIANTS = [
    ("q4_0", "Q4_0", "WAI-NSFW-illustrious-SDXL-v110-Q4_0.gguf", 1491343328, False, "needs_validation"),
    ("q4_1", "Q4_1", "WAI-NSFW-illustrious-SDXL-v110-Q4_1.gguf", 1649768928, False, "needs_validation"),
    ("q4_k_s", "Q4_K_S", "WAI-NSFW-illustrious-SDXL-v110-Q4_K_S.gguf", 1491773408, False, "needs_validation"),
    ("q5_0", "Q5_0", "WAI-NSFW-illustrious-SDXL-v110-Q5_0.gguf", 1808194528, False, "needs_validation"),
    ("q5_k_m", "Q5_K_M", "WAI-NSFW-illustrious-SDXL-v110-Q5_K_M.gguf", 1844424928, False, "needs_validation"),
    ("q5_k_s", "Q5_K_S", "WAI-NSFW-illustrious-SDXL-v110-Q5_K_S.gguf", 1808194528, False, "needs_validation"),
    ("q6_k", "Q6_K", "WAI-NSFW-illustrious-SDXL-v110-Q6_K.gguf", 2144848928, False, "needs_validation"),
    ("q8_0", "Q8_0", "WAI-NSFW-illustrious-SDXL-v110-Q8_0.gguf", 2758748128, True, "verified"),
    ("f16", "F16", "WAI-NSFW-illustrious-SDXL-v110-F16.gguf", 5135086048, False, "needs_validation"),
]


_SOTHMIK_WAI_V140_VARIANTS = [
    ("q8_0", "Q8_0", "waiNSFWIllustrious_v140-Q8_0.gguf", 2736430048, True, "verified"),
]


_CALCUIS_ILLUSTRIOUS_VARIANTS = [
    ("q4_0", "Q4_0", "illustrious-q4_0.gguf", 1457146848, True, "verified_q4_smoke"),
    ("q5_0", "Q5_0", "illustrious-q5_0.gguf", 1776967648, False, "mapped_needs_validation"),
    ("q8_0", "Q8_0", "illustrious-q8_0.gguf", 2736430048, False, "mapped_needs_validation"),
    ("f16", "F16", "illustrious-f16.gguf", 5135086048, False, "mapped_high_vram"),
]


_BTASKEL_ILLUSTRIOUS_V20_VARIANTS = [
    ("q8_0", "Q8_0", "illustriousXLV20_v20Stable-Q8_0.gguf", 2758748128, False, "failed_visual_reprobe"),
]


_DIVING_ILLUSTRIOUS_FLAT_VARIANTS = [
    ("q4_k_m", "Q4_K_M", "diving-illustrious-flat-anime-paradigm-shift.Q4_K_M.gguf", 1446633120, True, "verified_q4_smoke"),
    ("q5_k_m", "Q5_K_M", "diving-illustrious-flat-anime-paradigm-shift.Q5_K_M.gguf", 1767368160, False, "mapped_needs_validation"),
    ("q8_0", "Q8_0", "diving-illustrious-flat-anime-paradigm-shift.Q8_0.gguf", 2729573280, False, "mapped_needs_validation"),
]


_SD35_VARIANTS = [
    ("q4_0", "Q4_0", "sd3.5_large-q4_0.gguf", 4772054752, False, "failed_visual_reprobe"),
    ("q4_1", "Q4_1", "sd3.5_large-q4_1.gguf", 5272949472, False, "disabled_abandoned"),
    ("q5_0", "Q5_0", "sd3.5_large-q5_0.gguf", 5773844192, False, "disabled_abandoned"),
    ("q5_1", "Q5_1", "sd3.5_large-q5_1.gguf", 6274738912, False, "disabled_abandoned"),
    ("q8_0", "Q8_0", "sd3.5_large-q8_0.gguf", 8779212512, False, "disabled_abandoned"),
    ("f16", "F16", "sd3.5_large-f16.gguf", 16292633312, False, "disabled_abandoned"),
]


def _variant(variant_id, label, filename, size_bytes, enabled, status):
    return {
        "id": variant_id,
        "label": label,
        "gguf_file": filename,
        "filename": filename,
        "size_bytes": int(size_bytes or 0),
        "enabled": bool(enabled),
        "status": status,
    }


OFFICIAL_GGUF_PROFILES = {
    "wai_illustrious_v110_sdxl": {
        "id": "wai_illustrious_v110_sdxl",
        "label": "WAI Illustrious SDXL v11",
        "family": "sdxl",
        "status": "verified",
        "enabled": True,
        "repo_id": "kekusprod/WAI-NSFW-illustrious-SDXL-v110-GGUF",
        "base_repo": "stabilityai/stable-diffusion-xl-base-1.0",
        "workflow_family": "sdxl_dual_clip_gguf",
        "clip_loader_class": "DualCLIPLoaderGGUF",
        "clip_type": "sdxl",
        "source_url": "https://huggingface.co/kekusprod/WAI-NSFW-illustrious-SDXL-v110-GGUF",
        "sampler_defaults": {"sampler_name": "euler", "scheduler": "normal", "cfg": 5.0, "steps": 24},
        "companions": [
            {
                "role": "clip_l",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_clip_l_fp8_e4m3fn.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name1",
            },
            {
                "role": "clip_g",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_clip_g_fp8_e4m3fn.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name2",
            },
            {
                "role": "vae",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_v110_vae_fp8_e4m3fn.safetensors",
                "model_type": "vae",
                "install_subdir": "vae",
                "slot": "vae_name",
            },
        ],
        "variants": [_variant(*item) for item in _WAI_VARIANTS],
    },
    "sothmik_wai_illustrious_v140_sdxl": {
        "id": "sothmik_wai_illustrious_v140_sdxl",
        "label": "WAI Illustrious SDXL v14 Q8",
        "family": "sdxl",
        "status": "verified",
        "enabled": True,
        "repo_id": "sothmik/Wai-NSFW-Illustrious-v140-Q8-GGUF",
        "base_repo": "dhead/wai-nsfw-illustrious-sdxl-v140-sdxl",
        "workflow_family": "sdxl_dual_clip_gguf",
        "clip_loader_class": "DualCLIPLoaderGGUF",
        "clip_type": "sdxl",
        "source_url": "https://huggingface.co/sothmik/Wai-NSFW-Illustrious-v140-Q8-GGUF",
        "sampler_defaults": {"sampler_name": "euler", "scheduler": "normal", "cfg": 5.0, "steps": 24},
        "companions": [
            {
                "role": "clip_l",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_clip_l_fp8_e4m3fn.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name1",
            },
            {
                "role": "clip_g",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_clip_g_fp8_e4m3fn.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name2",
            },
            {
                "role": "vae",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_v110_vae_fp8_e4m3fn.safetensors",
                "model_type": "vae",
                "install_subdir": "vae",
                "slot": "vae_name",
            },
        ],
        "variants": [_variant(*item) for item in _SOTHMIK_WAI_V140_VARIANTS],
    },
    "calcuis_illustrious_sdxl": {
        "id": "calcuis_illustrious_sdxl",
        "label": "Calcuis Illustrious SDXL GGUF",
        "family": "sdxl",
        "status": "verified_q4_smoke",
        "enabled": True,
        "repo_id": "calcuis/illustrious",
        "base_repo": "OnomaAIResearch/Illustrious-xl-early-release-v0",
        "workflow_family": "sdxl_dual_clip_gguf",
        "clip_loader_class": "DualCLIPLoader",
        "clip_type": "sdxl",
        "source_url": "https://huggingface.co/calcuis/illustrious",
        "prompt_style_hint": "Illustrious anime test pack; use score/source_anime quality tags for closer model-card behavior.",
        "sampler_defaults": {"sampler_name": "euler", "scheduler": "normal", "cfg": 8.0, "steps": 20},
        "companions": [
            {
                "role": "clip_l",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_clip_l.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name1",
            },
            {
                "role": "clip_g",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_clip_g.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name2",
            },
            {
                "role": "vae",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_vae.safetensors",
                "model_type": "vae",
                "install_subdir": "vae",
                "slot": "vae_name",
            },
        ],
        "variants": [_variant(*item) for item in _CALCUIS_ILLUSTRIOUS_VARIANTS],
    },
    "btaskel_illustrious_xl_v20_sdxl": {
        "id": "btaskel_illustrious_xl_v20_sdxl",
        "label": "Illustrious XL v2.0 GGUF",
        "family": "sdxl",
        "status": "failed_visual_reprobe",
        "enabled": False,
        "hidden": True,
        "disabled_reason": "生成圖異常，已放棄 btaskel Illustrious XL v2.0 GGUF 支援",
        "repo_id": "btaskel/Illustrious-XL-v2.0-GGUF",
        "base_repo": "OnomaAIResearch/Illustrious-XL-v2.0",
        "workflow_family": "sdxl_dual_clip_gguf",
        "clip_loader_class": "DualCLIPLoader",
        "clip_type": "sdxl",
        "source_url": "https://huggingface.co/btaskel/Illustrious-XL-v2.0-GGUF",
        "prompt_style_hint": "Remote reprobe 2026-05-29 completed but visual output was judged abnormal; hidden from public options and kept only as an internal failed-record profile.",
        "sampler_defaults": {"sampler_name": "euler", "scheduler": "normal", "cfg": 6.0, "steps": 24},
        "companions": [
            {
                "role": "clip_l",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_clip_l.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name1",
            },
            {
                "role": "clip_g",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_clip_g.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name2",
            },
            {
                "role": "vae",
                "repo_id": "calcuis/illustrious",
                "filename": "illustrious_vae.safetensors",
                "model_type": "vae",
                "install_subdir": "vae",
                "slot": "vae_name",
            },
        ],
        "variants": [_variant(*item) for item in _BTASKEL_ILLUSTRIOUS_V20_VARIANTS],
    },
    "diving_illustrious_flat_anime_sdxl": {
        "id": "diving_illustrious_flat_anime_sdxl",
        "label": "Diving Illustrious Flat Anime GGUF",
        "family": "sdxl",
        "status": "verified_q4_smoke",
        "enabled": True,
        "repo_id": "void-gryph/diving-illustrious-flat-anime-paradigm-shift-GGUF",
        "base_repo": "stabilityai/stable-diffusion-xl-base-1.0",
        "workflow_family": "sdxl_dual_clip_gguf",
        "clip_loader_class": "DualCLIPLoader",
        "clip_type": "sdxl",
        "source_url": "https://huggingface.co/void-gryph/diving-illustrious-flat-anime-paradigm-shift-GGUF",
        "prompt_style_hint": "Model-card prompt hint: (anime coloring, anime screencap:1.5); recommended native test size 896x1152.",
        "sampler_defaults": {"sampler_name": "euler_ancestral", "scheduler": "karras", "cfg": 5.5, "steps": 25},
        "companions": [
            {
                "role": "clip_l",
                "repo_id": "void-gryph/diving-illustrious-flat-anime-paradigm-shift-GGUF",
                "filename": "clip_l.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name1",
            },
            {
                "role": "clip_g",
                "repo_id": "void-gryph/diving-illustrious-flat-anime-paradigm-shift-GGUF",
                "filename": "clip_g.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name2",
            },
            {
                "role": "vae",
                "repo_id": "void-gryph/diving-illustrious-flat-anime-paradigm-shift-GGUF",
                "filename": "vae.safetensors",
                "model_type": "vae",
                "install_subdir": "vae",
                "slot": "vae_name",
            },
        ],
        "variants": [_variant(*item) for item in _DIVING_ILLUSTRIOUS_FLAT_VARIANTS],
    },
    "sd35_large_gguf": {
        "id": "sd35_large_gguf",
        "label": "Stable Diffusion 3.5 Large GGUF",
        "family": "sd3.5",
        "status": "failed_visual_reprobe",
        "enabled": False,
        "hidden": True,
        "disabled_reason": "生成圖異常，已放棄 SD35 GGUF 支援",
        "repo_id": "calcuis/sd3.5-large-gguf",
        "base_repo": "stabilityai/stable-diffusion-3.5-large",
        "workflow_family": "sd3_triple_clip_gguf",
        "clip_loader_class": "TripleCLIPLoader",
        "clip_type": "sd3",
        "source_url": "https://huggingface.co/calcuis/sd3.5-large-gguf",
        "prompt_style_hint": "生成圖異常，已從前台可選 GGUF profile 清除；保留 disabled 記錄只用於阻擋舊請求與標示已安裝殘留檔。",
        "native_resolution_policy": {
            "max_megapixels": 1.05,
            "multiple_of": 64,
            "output_scale_node": "ImageScale",
            "output_upscale_method": "lanczos",
        },
        "sampler_defaults": {
            "sampler_name": "dpmpp_2m",
            "scheduler": "sgm_uniform",
            "cfg": 4.5,
            "steps": 40,
            "sd3_shift": 3.0,
            "sd3_negative_split": 0.1,
        },
        "companions": [
            {
                "role": "clip_g",
                "repo_id": "calcuis/sd3.5-large-gguf",
                "filename": "clip_g.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name1",
            },
            {
                "role": "clip_l",
                "repo_id": "calcuis/sd3.5-large-gguf",
                "filename": "clip_l.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name2",
            },
            {
                "role": "t5",
                "repo_id": "calcuis/sd3.5-large-gguf",
                "filename": "t5xxl_fp8_e4m3fn.safetensors",
                "model_type": "clip",
                "install_subdir": "text_encoders",
                "slot": "clip_name3",
            },
            {
                "role": "vae",
                "repo_id": "calcuis/sd3.5-large-gguf",
                "filename": "diffusion_pytorch_model.safetensors",
                "model_type": "vae",
                "install_subdir": "vae",
                "slot": "vae_name",
            },
        ],
        "variants": [_variant(*item) for item in _SD35_VARIANTS],
    },
}


def _copy_profile(profile):
    return deepcopy(profile) if isinstance(profile, dict) else None


def official_gguf_profiles(*, include_disabled=True):
    profiles = []
    for profile in OFFICIAL_GGUF_PROFILES.values():
        if include_disabled or profile.get("enabled"):
            profiles.append(_copy_profile(profile))
    return sorted(profiles, key=lambda item: (not item.get("enabled"), str(item.get("label") or "")))


def get_official_gguf_profile(profile_id, *, include_disabled=True):
    profile = OFFICIAL_GGUF_PROFILES.get(str(profile_id or "").strip())
    if not profile:
        return None
    if not include_disabled and not profile.get("enabled"):
        return None
    return _copy_profile(profile)


def _variant_matches(variant, value):
    raw = str(value or "").strip()
    if not raw:
        return False
    name = PurePosixPath(raw.replace("\\", "/")).name
    return raw == variant.get("id") or raw == variant.get("gguf_file") or name == PurePosixPath(variant.get("gguf_file") or "").name


def get_official_gguf_variant(profile, variant_id="", gguf_file="", *, require_enabled=True):
    if not isinstance(profile, dict):
        return None
    candidates = [variant_id, gguf_file]
    enabled_profile = bool(profile.get("enabled"))
    for variant in profile.get("variants") or []:
        if not isinstance(variant, dict):
            continue
        if any(_variant_matches(variant, value) for value in candidates):
            if require_enabled and (not enabled_profile or not variant.get("enabled")):
                return None
            return deepcopy(variant)
    enabled_variants = [
        deepcopy(variant)
        for variant in (profile.get("variants") or [])
        if isinstance(variant, dict) and (not require_enabled or (enabled_profile and variant.get("enabled")))
    ]
    return enabled_variants[0] if not any(candidates) and len(enabled_variants) == 1 else None


def resolve_official_gguf_selection(profile_id="", variant_id="", *, repo_id="", gguf_file="", require_enabled=True):
    profile = get_official_gguf_profile(profile_id, include_disabled=not require_enabled)
    if profile:
        variant = get_official_gguf_variant(profile, variant_id, gguf_file, require_enabled=require_enabled)
        return (profile, variant) if variant else (profile, None)
    repo_text = str(repo_id or "").strip()
    file_text = str(gguf_file or "").strip()
    if not repo_text or not file_text:
        return None, None
    for candidate in official_gguf_profiles(include_disabled=not require_enabled):
        if str(candidate.get("repo_id") or "") != repo_text:
            continue
        variant = get_official_gguf_variant(candidate, variant_id, file_text, require_enabled=require_enabled)
        if variant:
            return candidate, variant
    return None, None


def gguf_profile_unavailable_message(profile, variant=None):
    if not isinstance(profile, dict):
        return "GGUF profile 尚未通過本站驗證，暫不開放。"
    profile_label = profile.get("label") or profile.get("id") or "GGUF profile"
    variant_label = ""
    if isinstance(variant, dict):
        variant_label = variant.get("label") or variant.get("id") or ""
    reason = ""
    for key in ("disabled_reason", "status"):
        for candidate in (variant, profile):
            if not isinstance(candidate, dict):
                continue
            reason = str(candidate.get(key) or "").strip()
            if reason:
                break
        if reason:
            break
    subject = f"GGUF profile「{profile_label}」"
    if variant_label:
        subject += f"的 {variant_label}"
    return f"{subject} 尚未通過本站驗證，暫不開放" + (f"：{reason}。" if reason else "。")


def _filename_basename(value):
    return PurePosixPath(str(value or "").strip().replace("\\", "/")).name


def _find_profile_variant_for_file(value):
    basename = _filename_basename(value)
    if not basename:
        return None, None
    for profile in official_gguf_profiles(include_disabled=True):
        for variant in profile.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            if _filename_basename(variant.get("gguf_file")) == basename:
                return profile, deepcopy(variant)
    return None, None


def installed_gguf_inventory(diffusion_models):
    result = []
    seen = set()
    for option in diffusion_models or []:
        raw = str(option or "").strip()
        if not raw or not raw.lower().endswith(".gguf"):
            continue
        key = raw.replace("\\", "/")
        if key in seen:
            continue
        seen.add(key)
        profile, variant = _find_profile_variant_for_file(raw)
        item = {
            "name": raw,
            "option": raw,
            "basename": _filename_basename(raw),
            "installed": True,
            "official_profile": bool(profile and variant),
            "status": "unmapped",
            "enabled": False,
        }
        if profile and variant:
            item.update({
                "official_profile": True,
                "profile_id": profile.get("id"),
                "profile_label": profile.get("label"),
                "profile_enabled": bool(profile.get("enabled")),
                "profile_status": profile.get("status"),
                "repo_id": profile.get("repo_id"),
                "source_url": profile.get("source_url"),
                "variant_id": variant.get("id"),
                "variant_label": variant.get("label"),
                "variant_enabled": bool(variant.get("enabled")),
                "variant_status": variant.get("status"),
                "gguf_file": variant.get("gguf_file"),
                "size_bytes": int(variant.get("size_bytes") or 0),
                "status": variant.get("status") or profile.get("status") or "mapped",
                "enabled": bool(profile.get("enabled") and variant.get("enabled")),
            })
        result.append(item)
    return sorted(result, key=lambda item: (not item.get("official_profile"), str(item.get("basename") or "").lower()))


def public_gguf_profiles():
    result = []
    for profile in official_gguf_profiles(include_disabled=True):
        if profile.get("hidden"):
            continue
        visible = {
            key: profile.get(key)
            for key in (
                "id",
                "label",
                "family",
                "status",
                "enabled",
                "hidden",
                "repo_id",
                "base_repo",
                "workflow_family",
                "clip_loader_class",
                "source_url",
                "prompt_style_hint",
                "sampler_defaults",
                "native_resolution_policy",
                "disabled_reason",
            )
        }
        visible["variants"] = [
            {
                key: variant.get(key)
                for key in ("id", "label", "gguf_file", "filename", "size_bytes", "enabled", "status")
            }
            for variant in (profile.get("variants") or [])
            if isinstance(variant, dict)
        ]
        visible["companions"] = [
            {
                key: item.get(key)
                for key in ("role", "repo_id", "filename", "model_type", "install_subdir", "slot")
            }
            for item in (profile.get("companions") or [])
            if isinstance(item, dict)
        ]
        result.append(visible)
    return result
