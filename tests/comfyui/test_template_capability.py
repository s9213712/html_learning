"""Capability check regression for the ComfyUI template importer (§6)."""

import pytest

from services.comfyui.template.analyzer import (
    NodeAnalysis,
    WorkflowAnalysis,
)
from services.comfyui.template.capability import (
    check_workflow_capability,
    embedding_option_available,
    iter_required_models,
    reset_object_info_cache,
    rewrite_workflow_model_inputs_to_local_options,
)


class _StubClient:
    """Minimal /object_info double; counts call sites for cache assertions."""

    def __init__(self, payload, *, base_url="http://stub", embeddings=None, latent_upscale_models=None):
        self._payload = payload
        self.base_url = base_url
        self._embeddings = list(embeddings or [])
        self._latent_upscale_models = list(latent_upscale_models or [])
        self.calls = 0

    def get_object_info(self):
        self.calls += 1
        return self._payload

    def get_embeddings(self):
        return list(self._embeddings)

    def get_latent_upscale_models(self):
        return list(self._latent_upscale_models)


def _local_payload(*, classes, models=None, samplers=None, schedulers=None):
    """Build the shape ComfyUI returns from /object_info for the classes we care about."""
    info = {cls: {"input": {"required": {}}} for cls in classes}
    if "CheckpointLoaderSimple" in info and models is not None:
        info["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"] = [list(models)]
    if "VAELoader" in info and models is not None:
        info["VAELoader"]["input"]["required"]["vae_name"] = [["bundled.vae.safetensors"]]
    if "LoraLoader" in info and models is not None:
        info["LoraLoader"]["input"]["required"]["lora_name"] = [["lcm.safetensors"]]
    for class_type in ("KSampler", "KSamplerAdvanced"):
        if class_type in info:
            info[class_type]["input"]["required"]["sampler_name"] = [list(samplers or ["euler", "dpmpp_2m"])]
            info[class_type]["input"]["required"]["scheduler"] = [list(schedulers or ["normal", "karras"])]
    return info


def _analysis(*, class_types, denied=None, models=None):
    a = WorkflowAnalysis()
    for cls in class_types:
        node = NodeAnalysis(node_id=str(len(a.nodes) + 1), class_type=cls)
        node.is_allowed = cls not in (denied or set())
        node.is_explicitly_denied = cls in (denied or set())
        node.is_unknown = not (node.is_allowed or node.is_explicitly_denied)
        a.nodes.append(node)
    a.class_types = set(class_types)
    a.allowed_classes = set(class_types) - set(denied or set())
    a.denied_classes = set(denied or set())
    a.unknown_classes = set()
    a.required_models = dict(models or {})
    return a


@pytest.fixture(autouse=True)
def _reset_cache():
    reset_object_info_cache()
    yield
    reset_object_info_cache()


def test_capability_supported_when_local_has_everything():
    info = _local_payload(
        classes={"CheckpointLoaderSimple", "CLIPTextEncode", "KSampler", "KSamplerAdvanced", "VAEDecode", "SaveImage"},
        models=["v1-5.safetensors", "another.safetensors"],
    )
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"CheckpointLoaderSimple", "CLIPTextEncode", "KSampler", "KSamplerAdvanced", "VAEDecode", "SaveImage"},
        models={"ckpt": ["v1-5.safetensors"]},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "SUPPORTED"
    assert cap.unsupported == []
    assert cap.missing_models == {}
    assert "euler" in cap.sampler_options["KSampler.sampler_name"]
    assert "euler" in cap.sampler_options["KSamplerAdvanced.sampler_name"]


def test_capability_unsupported_when_local_missing_class():
    info = _local_payload(classes={"CheckpointLoaderSimple", "KSampler"})
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"CheckpointLoaderSimple", "KSampler", "FancyCommunityNode"},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "UNSUPPORTED"
    assert "FancyCommunityNode" in cap.unsupported
    assert "CheckpointLoaderSimple" in cap.supported


def test_capability_partially_supported_when_only_models_missing():
    info = _local_payload(
        classes={"CheckpointLoaderSimple", "KSampler"},
        models=["other.safetensors"],
    )
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"CheckpointLoaderSimple", "KSampler"},
        models={"ckpt": ["v1-5.safetensors"]},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "PARTIALLY_SUPPORTED"
    assert cap.missing_models == {"ckpt": ["v1-5.safetensors"]}
    assert cap.unsupported == []


def test_capability_checks_diffusion_model_and_clip_buckets():
    info = {
        "UNETLoader": {
            "input": {"required": {"unet_name": [["anima-preview2.safetensors"]]}}
        },
        "CLIPLoader": {
            "input": {"required": {"clip_name": [["qwen_3_06b_base.safetensors"]]}}
        },
    }
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"UNETLoader", "CLIPLoader"},
        models={
            "diffusion_model": ["anima-preview2.safetensors"],
            "clip": ["qwen_3_06b_base.safetensors"],
        },
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "SUPPORTED"
    assert cap.missing_models == {}


def test_capability_checks_ltx_checkpoint_loader_buckets():
    info = {
        "LTXVAudioVAELoader": {
            "input": {"required": {"ckpt_name": [["video/ltx-2.3-22b-dev-fp8.safetensors"]]}}
        },
        "LTXAVTextEncoderLoader": {
            "input": {"required": {"ckpt_name": [["video/ltx-2.3-22b-dev-fp8.safetensors"]]}}
        },
    }
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"LTXVAudioVAELoader", "LTXAVTextEncoderLoader"},
        models={"ckpt": ["ltx-2.3-22b-dev-fp8.safetensors"]},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "SUPPORTED"
    assert cap.missing_models == {}


def test_capability_checks_comfyui_gguf_unet_loader_bucket():
    info = {
        "UnetLoaderGGUF": {
            "input": {"required": {"unet_name": [["WAI-NSFW-Illustrious-v140-Q8_0.gguf"]]}}
        },
        "DualCLIPLoader": {
            "input": {
                "required": {
                    "clip_name1": [["clip_l.safetensors"]],
                    "clip_name2": [["clip_g.safetensors"]],
                }
            }
        },
    }
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"UnetLoaderGGUF", "DualCLIPLoader"},
        models={
            "diffusion_model": ["WAI-NSFW-Illustrious-v140-Q8_0.gguf"],
            "clip": ["clip_l.safetensors", "clip_g.safetensors"],
        },
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "SUPPORTED"
    assert cap.missing_models == {}


def test_capability_checks_clip_vision_bucket_against_clip_vision_loader_options():
    info = {
        "CLIPVisionLoader": {
            "input": {"required": {"clip_name": [["sigclip_vision_patch14_384.safetensors"]]}}
        },
    }
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"CLIPVisionLoader"},
        models={"clip_vision": ["sigclip_vision_patch14_384.safetensors"]},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "SUPPORTED"
    assert cap.missing_models == {}


def test_capability_checks_latent_upscale_bucket_against_latent_loader_options():
    info = {
        "LatentUpscaleModelLoader": {
            "input": {
                "required": {
                    "model_name": [["3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"]]
                }
            }
        },
    }
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"LatentUpscaleModelLoader"},
        models={"latent_upscale_model": ["ltx-2.3-spatial-upscaler-x2-1.1.safetensors"]},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "SUPPORTED"
    assert cap.missing_models == {}


def test_capability_falls_back_to_latent_upscale_model_folder_catalog():
    info = {
        "LatentUpscaleModelLoader": {
            "input": {
                "required": {
                    "model_name": ["COMBO", {"options": []}]
                }
            }
        },
    }
    client = _StubClient(
        info,
        latent_upscale_models=["3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"],
    )
    analysis = _analysis(
        class_types={"LatentUpscaleModelLoader"},
        models={"latent_upscale_model": ["ltx-2.3-spatial-upscaler-x2-1.1.safetensors"]},
    )

    cap = check_workflow_capability(analysis, client=client)

    assert cap.overall == "SUPPORTED"
    assert cap.missing_models == {}


def test_rewrite_workflow_model_inputs_uses_unique_subfolder_basename_match():
    info = {
        "LatentUpscaleModelLoader": {
            "input": {
                "required": {
                    "model_name": [["3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"]]
                }
            }
        },
    }
    workflow = {
        "303": {
            "class_type": "LatentUpscaleModelLoader",
            "inputs": {"model_name": "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"},
        },
    }
    patched = rewrite_workflow_model_inputs_to_local_options(workflow, client=_StubClient(info))
    assert patched["303"]["inputs"]["model_name"] == "3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"
    assert workflow["303"]["inputs"]["model_name"] == "ltx-2.3-spatial-upscaler-x2-1.1.safetensors"


def test_capability_ignores_paid_api_model_bucket_as_non_local_file():
    info = {"ByteDanceSeedreamNode": {"input": {"required": {}}}}
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"ByteDanceSeedreamNode"},
        models={"api_model": ["seedream-5-lite"]},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "SUPPORTED"
    assert cap.missing_models == {}


def test_capability_checks_embedding_bucket_with_embedding_catalog():
    info = _local_payload(classes={"CLIPTextEncode"})
    client = _StubClient(info, embeddings=["easynegative.safetensors"])
    analysis = _analysis(
        class_types={"CLIPTextEncode"},
        models={"embedding": ["easynegative.safetensors", "badhandv4.pt"]},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "PARTIALLY_SUPPORTED"
    assert cap.missing_models == {"embedding": ["badhandv4.pt"]}


def test_capability_treats_builtin_vae_sentinel_as_checkpoint_embedded():
    info = _local_payload(classes={"CheckpointLoaderSimple", "VAELoader"}, models=["base.safetensors"])
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"CheckpointLoaderSimple", "VAELoader"},
        models={"ckpt": ["base.safetensors"], "vae": ["__checkpoint_builtin__"]},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "SUPPORTED"
    assert cap.missing_models == {}


def test_capability_matches_embedding_subfolders_spaces_and_optional_extensions():
    assert embedding_option_available(
        "lazy series\\IL\\lazyneg",
        ["lazy series/IL/lazyneg.safetensors"],
    )
    assert embedding_option_available(
        "lazy series\\IL\\lazyneg",
        ["lazyneg.pt"],
    )

    info = _local_payload(classes={"CLIPTextEncode"})
    client = _StubClient(info, embeddings=["lazy series/IL/lazyneg.safetensors"])
    analysis = _analysis(
        class_types={"CLIPTextEncode"},
        models={"embedding": ["lazy series\\IL\\lazyneg"]},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "SUPPORTED"
    assert cap.missing_models == {}


def test_capability_explicit_denied_class_treated_as_unsupported():
    """Per §4: denied classes never reach Run; capability surfaces them as UNSUPPORTED."""
    info = _local_payload(
        classes={"CheckpointLoaderSimple", "KSampler", "ReActorFaceSwap"},
    )
    client = _StubClient(info)
    analysis = _analysis(
        class_types={"CheckpointLoaderSimple", "KSampler", "ReActorFaceSwap"},
        denied={"ReActorFaceSwap"},
    )
    cap = check_workflow_capability(analysis, client=client)
    assert cap.overall == "UNSUPPORTED"
    assert "ReActorFaceSwap" in cap.unsupported
    assert any("ReActorFaceSwap" in b for b in cap.blockers)


def test_capability_client_none_yields_unsupported_with_blocker():
    analysis = _analysis(class_types={"KSampler"})
    cap = check_workflow_capability(analysis, client=None)
    assert cap.overall == "UNSUPPORTED"
    assert cap.unsupported == ["KSampler"]
    assert cap.blockers and "ComfyUI" in cap.blockers[0]


def test_capability_client_raise_yields_unsupported_with_blocker():
    class _Failing:
        base_url = "http://stub-failing"
        def get_object_info(self):
            raise RuntimeError("connection refused")
    analysis = _analysis(class_types={"KSampler"})
    cap = check_workflow_capability(analysis, client=_Failing())
    assert cap.overall == "UNSUPPORTED"
    assert any("無法取得" in b for b in cap.blockers)


def test_capability_caches_object_info_within_ttl():
    info = _local_payload(classes={"KSampler"})
    client = _StubClient(info)
    analysis = _analysis(class_types={"KSampler"})
    check_workflow_capability(analysis, client=client)
    check_workflow_capability(analysis, client=client)
    check_workflow_capability(analysis, client=client)
    assert client.calls == 1, "object_info should be hit once per (client base_url, ttl) window"


def test_capability_to_dict_shape_is_json_friendly():
    info = _local_payload(classes={"KSampler"})
    client = _StubClient(info)
    analysis = _analysis(class_types={"KSampler"})
    cap = check_workflow_capability(analysis, client=client)
    payload = cap.to_dict()
    assert payload["overall"] in {"SUPPORTED", "PARTIALLY_SUPPORTED", "UNSUPPORTED"}
    # `partial` becomes [{"class_type":..,"reason":..}, ...]
    assert isinstance(payload["partial"], list)
    assert isinstance(payload["sampler_options"], dict)


def test_iter_required_models_yields_pairs():
    a = _analysis(
        class_types={"CheckpointLoaderSimple", "LoraLoader"},
        models={"ckpt": ["a.safetensors"], "lora": ["b.safetensors", "c.safetensors"]},
    )
    pairs = sorted(iter_required_models(a))
    assert pairs == [
        ("ckpt", "a.safetensors"),
        ("lora", "b.safetensors"),
        ("lora", "c.safetensors"),
    ]
