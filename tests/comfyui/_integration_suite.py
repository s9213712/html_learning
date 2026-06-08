import io
import importlib.util
import json
import sqlite3
import threading
import time
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask, jsonify, make_response, request

from routes import comfyui as comfyui_routes
from routes.comfyui import register_comfyui_routes
from routes.comfyui_sections import workflow_routes as comfyui_workflow_routes
from services.comfyui import client as comfyui_client_module
from services.comfyui import execution as comfy_execution
from services.storage.cloud_drive import ensure_cloud_drive_attachment_schema
from services.comfyui.client import ComfyUIClient, ComfyUIError, ComfyUIImage
from services.users.member_levels import ensure_member_level_rules_schema
from services.storage.storage_albums import (
    create_album,
    create_storage_file_entry,
    create_storage_folder,
    ensure_output_album,
    ensure_storage_album_schema,
    move_storage_file,
)
from services.security.upload_security import create_uploaded_file_record
from services.security.upload_security import ensure_upload_security_schema, update_cloud_drive_security_policy


ROOT = Path(__file__).resolve().parents[2]
WAI_GGUF_PROFILE_ID = "wai_illustrious_v110_sdxl"
WAI_GGUF_VARIANT_ID = "q8_0"
WAI_GGUF_REPO = "kekusprod/WAI-NSFW-illustrious-SDXL-v110-GGUF"
WAI_GGUF_FILE = "WAI-NSFW-illustrious-SDXL-v110-Q8_0.gguf"
WAI_CLIP_L = "illustrious_clip_l_fp8_e4m3fn.safetensors"
WAI_CLIP_G = "illustrious_clip_g_fp8_e4m3fn.safetensors"
WAI_VAE = "illustrious_v110_vae_fp8_e4m3fn.safetensors"
SOTHMIK_WAI_GGUF_PROFILE_ID = "sothmik_wai_illustrious_v140_sdxl"
SOTHMIK_WAI_GGUF_VARIANT_ID = "q8_0"
SOTHMIK_WAI_GGUF_REPO = "sothmik/Wai-NSFW-Illustrious-v140-Q8-GGUF"
SOTHMIK_WAI_GGUF_FILE = "waiNSFWIllustrious_v140-Q8_0.gguf"
CALCUIS_ILLUSTRIOUS_GGUF_PROFILE_ID = "calcuis_illustrious_sdxl"
CALCUIS_ILLUSTRIOUS_GGUF_FILE = "illustrious-q4_0.gguf"
BTASKEL_ILLUSTRIOUS_GGUF_PROFILE_ID = "btaskel_illustrious_xl_v20_sdxl"
BTASKEL_ILLUSTRIOUS_GGUF_VARIANT_ID = "q8_0"
BTASKEL_ILLUSTRIOUS_GGUF_REPO = "btaskel/Illustrious-XL-v2.0-GGUF"
BTASKEL_ILLUSTRIOUS_GGUF_FILE = "illustriousXLV20_v20Stable-Q8_0.gguf"
DIVING_ILLUSTRIOUS_GGUF_PROFILE_ID = "diving_illustrious_flat_anime_sdxl"
DIVING_ILLUSTRIOUS_GGUF_FILE = "diving-illustrious-flat-anime-paradigm-shift.Q4_K_M.gguf"
SD35_GGUF_PROFILE_ID = "sd35_large_gguf"
SD35_GGUF_VARIANT_ID = "q4_0"
SD35_GGUF_REPO = "calcuis/sd3.5-large-gguf"
SD35_GGUF_FILE = "sd3.5_large-q4_0.gguf"
SD35_CLIP_L = "clip_l.safetensors"
SD35_CLIP_G = "clip_g.safetensors"
SD35_T5 = "t5xxl_fp8_e4m3fn.safetensors"
SD35_VAE = "diffusion_pytorch_model.safetensors"


@pytest.fixture(autouse=True)
def _isolate_comfyui_client_object_info_cache():
    with comfyui_client_module._OBJECT_INFO_LOCK:
        comfyui_client_module._OBJECT_INFO_CACHE.clear()
    yield
    with comfyui_client_module._OBJECT_INFO_LOCK:
        comfyui_client_module._OBJECT_INFO_CACHE.clear()


class FakeComfyUIClient:
    base_url = "http://fake-comfyui"
    last_timeout_seconds = None
    last_expected_count = None
    last_params = {}
    last_workflow = {}
    last_wait_until_completed = None
    discarded = []
    interrupted = 0
    generated_count = 0
    generated_workflows = []
    uploaded_images = []

    def health_check(self, *, timeout=3):
        return {"ok": True, "system": {"os": "test"}}

    def get_models(self):
        return ["dream.safetensors", "photo.ckpt"]

    def get_loras(self):
        return ["detail.safetensors", "anime-style.safetensors"]

    def get_vaes(self):
        return ["sdxl_vae.safetensors", "anime_vae.pt"]

    def get_clip_vision_models(self):
        return ["sigclip_vision_patch14_384.safetensors"]

    def get_embeddings(self):
        return ["badhandv4.pt", "easynegative.safetensors"]

    def get_sampler_options(self):
        return {"samplers": ["euler", "dpmpp_2m"], "schedulers": ["normal", "karras"]}

    def get_capabilities(self):
        return {
            "available_nodes": [
                "CheckpointLoaderSimple",
                "VAELoader",
                "CLIPTextEncode",
                "KSampler",
                "VAEDecode",
                "SaveImage",
                "PreviewImage",
                "EmptyLatentImage",
                "LoadImage",
                "LoadImageMask",
                "VAEEncode",
                "VAEEncodeForInpaint",
                "ImagePadForOutpaint",
                "UpscaleModelLoader",
                "ImageUpscaleWithModel",
                "ControlNetLoader",
                "ControlNetApplyAdvanced",
                "CannyEdgePreprocessor",
                "DepthAnythingPreprocessor",
                "OpenposePreprocessor",
                "LineArtPreprocessor",
                "PiDiNetPreprocessor",
                "SoftEdgePreprocessor",
                "TilePreprocessor",
                "CLIPVisionLoader",
            ],
            "controlnet_models": [
                "control_v11p_sd15_canny.safetensors",
                "control_v11f1p_sd15_depth.safetensors",
                "control_v11p_sd15_openpose.safetensors",
                "control_v11p_sd15_lineart.safetensors",
                "control_v11p_sd15_scribble.safetensors",
                "control_v11p_sd15_softedge.safetensors",
                "control_v11f1e_sd15_tile.safetensors",
            ],
            "upscale_models": ["4x-UltraSharp.pth", "RealESRGAN_x4plus.pth"],
            "clip_vision_models": ["sigclip_vision_patch14_384.safetensors"],
            "controlnet_types": {
                "canny": {
                    "label": "Canny",
                    "available": True,
                    "available_preprocessors": ["CannyEdgePreprocessor"],
                    "default_preprocessor": "CannyEdgePreprocessor",
                    "matching_models": ["control_v11p_sd15_canny.safetensors"],
                },
                "depth": {
                    "label": "Depth",
                    "available": True,
                    "available_preprocessors": ["DepthAnythingPreprocessor"],
                    "default_preprocessor": "DepthAnythingPreprocessor",
                    "matching_models": ["control_v11f1p_sd15_depth.safetensors"],
                },
                "openpose": {
                    "label": "OpenPose",
                    "available": True,
                    "available_preprocessors": ["OpenposePreprocessor"],
                    "default_preprocessor": "OpenposePreprocessor",
                    "matching_models": ["control_v11p_sd15_openpose.safetensors"],
                },
                "lineart": {
                    "label": "Lineart",
                    "available": True,
                    "available_preprocessors": ["LineArtPreprocessor"],
                    "default_preprocessor": "LineArtPreprocessor",
                    "matching_models": ["control_v11p_sd15_lineart.safetensors"],
                },
                "scribble": {
                    "label": "Scribble",
                    "available": True,
                    "available_preprocessors": ["PiDiNetPreprocessor"],
                    "default_preprocessor": "PiDiNetPreprocessor",
                    "matching_models": ["control_v11p_sd15_scribble.safetensors"],
                },
                "softedge": {
                    "label": "SoftEdge",
                    "available": True,
                    "available_preprocessors": ["SoftEdgePreprocessor"],
                    "default_preprocessor": "SoftEdgePreprocessor",
                    "matching_models": ["control_v11p_sd15_softedge.safetensors"],
                },
                "tile": {
                    "label": "Tile",
                    "available": True,
                    "available_preprocessors": ["TilePreprocessor"],
                    "default_preprocessor": "TilePreprocessor",
                    "matching_models": ["control_v11f1e_sd15_tile.safetensors"],
                },
            },
            "generation_modes": [
                {"key": "txt2img", "label": "文字生圖", "available": True},
                {"key": "img2img", "label": "圖生圖", "available": True},
                {"key": "inpaint", "label": "局部重繪", "available": True},
                {"key": "outpaint", "label": "向外延展", "available": True},
                {"key": "upscale", "label": "放大修復", "available": True},
                {"key": "t2v", "label": "文字生影片", "available": True, "workflow_only": True},
                {"key": "i2v", "label": "圖片生影片", "available": True, "workflow_only": True},
                {"key": "v2v", "label": "影片轉影片", "available": True, "workflow_only": True},
                {"key": "t2s", "label": "文字生語音", "available": True, "workflow_only": True},
                {"key": "t2sv", "label": "文字生語音影片", "available": True, "workflow_only": True},
            ],
            "model_families": [
                {"key": "flux", "label": "FLUX", "installed": True, "matches": ["dream.safetensors"]},
                {"key": "wan", "label": "Wan Video", "installed": False, "matches": []},
            ],
        }

    def upload_image_bytes(self, data, filename, *, image_type="input", overwrite=False, subfolder=""):
        image_ref = {"filename": filename, "subfolder": subfolder, "type": image_type}
        FakeComfyUIClient.uploaded_images.append({"image_ref": dict(image_ref), "size": len(data or b"")})
        return image_ref

    _build_text_to_image_base = ComfyUIClient._build_text_to_image_base
    _attach_controlnet = ComfyUIClient._attach_controlnet
    build_text_to_image_workflow = ComfyUIClient.build_text_to_image_workflow
    build_image_to_image_workflow = ComfyUIClient.build_image_to_image_workflow
    build_inpaint_workflow = ComfyUIClient.build_inpaint_workflow
    build_outpaint_workflow = ComfyUIClient.build_outpaint_workflow
    build_upscale_workflow = ComfyUIClient.build_upscale_workflow
    build_generation_workflow = ComfyUIClient.build_generation_workflow

    def generate_image(self, params, *, timeout_seconds=180, progress_callback=None):
        FakeComfyUIClient.generated_count += 1
        FakeComfyUIClient.last_timeout_seconds = timeout_seconds
        FakeComfyUIClient.last_params = dict(params)
        if progress_callback:
            progress_callback({
                "phase": "running",
                "percent": 50,
                "current": 10,
                "max": 20,
                "current_node": "3",
                "detail": "節點 3：10/20",
                "queue_remaining": 0,
            })
        batch_size = int(params.get("batch_size") or 1)
        images = []
        for index in range(batch_size):
            images.append({
                "image_ref": {"filename": f"hackme_web_{index + 1:05d}_.png", "subfolder": "", "type": "output"},
                "mime_type": "image/png",
                "data": f"fake-png-bytes-{index + 1}".encode("utf-8"),
            })
        return {
            "prompt_id": "prompt-1",
            "image_ref": images[0]["image_ref"],
            "mime_type": images[0]["mime_type"],
            "data": images[0]["data"],
            "images": images,
        }

    def generate_from_workflow(self, workflow, *, timeout_seconds=180, expected_count=1, progress_callback=None, wait_until_completed=False):
        FakeComfyUIClient.generated_count += 1
        FakeComfyUIClient.last_timeout_seconds = timeout_seconds
        FakeComfyUIClient.last_expected_count = expected_count
        FakeComfyUIClient.last_workflow = json.loads(json.dumps(workflow))
        FakeComfyUIClient.last_wait_until_completed = bool(wait_until_completed)
        FakeComfyUIClient.generated_workflows.append(FakeComfyUIClient.last_workflow)
        if progress_callback:
            progress_callback({
                "phase": "running",
                "percent": 50,
                "current": 1,
                "max": max(1, int(expected_count or 1)),
                "current_node": "workflow",
                "detail": "workflow 執行中",
                "queue_remaining": 0,
            })
        batch_size = max(1, int(expected_count or 1))
        images = []
        for index in range(batch_size):
            images.append({
                "image_ref": {"filename": f"hackme_web_workflow_{index + 1:05d}_.png", "subfolder": "", "type": "output"},
                "mime_type": "image/png",
                "data": f"fake-workflow-png-bytes-{index + 1}".encode("utf-8"),
            })
        return {
            "prompt_id": "workflow-prompt-1",
            "image_ref": images[0]["image_ref"],
            "mime_type": images[0]["mime_type"],
            "data": images[0]["data"],
            "images": images,
        }

    def fetch_image(self, image_ref):
        return ComfyUIImage(
            filename=image_ref.get("filename") or "hackme_web_00001_.png",
            subfolder=image_ref.get("subfolder") or "",
            type=image_ref.get("type") or "output",
            mime_type="image/png",
            data=b"fake-png-bytes",
        )

    def fetch_file(self, file_ref):
        return ComfyUIImage(
            filename=file_ref.get("filename") or "hackme_web_00001_.png",
            subfolder=file_ref.get("subfolder") or "",
            type=file_ref.get("type") or "output",
            mime_type="image/png",
            data=b"fake-png-bytes",
        )

    def discard_image(self, image_ref, *, prompt_id=None, **kwargs):
        FakeComfyUIClient.discarded.append({"image_ref": dict(image_ref), "prompt_id": prompt_id, **kwargs})
        return {"file_deleted": True, "file_missing": False, "file_delete_supported": True, "history_deleted": bool(prompt_id)}

    def interrupt(self):
        FakeComfyUIClient.interrupted += 1
        return {"interrupted": True}


class FakeDiffusersBackendClient(FakeComfyUIClient):
    backend_kind = "diffusers"
    base_url = "diffusers://local/dhead%2FwaiIllustriousSDXL_v150"
    last_params = {}

    def get_models(self):
        return ["dhead/waiIllustriousSDXL_v150"]

    def get_loras(self):
        return []

    def get_vaes(self):
        return []

    def get_embeddings(self):
        return []

    def get_sampler_options(self):
        return {"samplers": ["diffusers-auto"], "schedulers": ["default"]}

    def get_capabilities(self):
        return {
            "backend_kind": "diffusers",
            "available_nodes": [],
            "controlnet_models": [],
            "upscale_models": [],
            "controlnet_types": {},
            "generation_modes": [
                {"key": "txt2img", "label": "文字生圖", "available": True},
                {"key": "img2img", "label": "圖生圖", "available": True},
                {"key": "inpaint", "label": "局部重繪", "available": True},
                {"key": "upscale", "label": "放大修復", "available": False},
                {"key": "t2v", "label": "文字生影片", "available": False, "workflow_only": True},
            ],
            "model_families": [{"key": "illustrious", "label": "Illustrious", "installed": True, "matches": ["dhead/waiIllustriousSDXL_v150"]}],
            "model_repo": "dhead/waiIllustriousSDXL_v150",
            "token_configured": True,
        }

    def generate_image(self, params, *, timeout_seconds=180, progress_callback=None, extra_data=None):
        FakeDiffusersBackendClient.last_params = dict(params)
        if progress_callback:
            progress_callback({"phase": "running", "percent": 40, "detail": "Diffusers 推論中"})
        return {
            "prompt_id": "diffusers-prompt-1",
            "image_ref": {"filename": "hackme_web_diffusers.png", "subfolder": "", "type": "output"},
            "mime_type": "image/png",
            "data": b"fake-diffusers-png",
            "images": [{
                "image_ref": {"filename": "hackme_web_diffusers.png", "subfolder": "", "type": "output"},
                "mime_type": "image/png",
                "data": b"fake-diffusers-png",
            }],
        }


class FakeNativeGgufComfyUIClient(FakeComfyUIClient):
    base_url = "http://127.0.0.1:8188"

    def get_capabilities(self):
        payload = super().get_capabilities()
        payload["available_nodes"] = sorted(set(payload["available_nodes"]) | {
            "UnetLoaderGGUF",
            "DualCLIPLoader",
            "DualCLIPLoaderGGUF",
            "VAELoader",
            "CLIPTextEncode",
            "EmptyLatentImage",
            "KSampler",
            "VAEDecode",
            "SaveImage",
        })
        payload["diffusion_models"] = [WAI_GGUF_FILE]
        payload["clip_models"] = [WAI_CLIP_L, WAI_CLIP_G]
        payload["vaes"] = [WAI_VAE]
        payload["samplers"] = ["euler", "dpmpp_2m"]
        payload["schedulers"] = ["normal", "karras"]
        return payload


class FakeSd35NativeGgufComfyUIClient(FakeComfyUIClient):
    base_url = "http://127.0.0.1:8188"

    def get_capabilities(self):
        payload = super().get_capabilities()
        payload["available_nodes"] = sorted(set(payload["available_nodes"]) | {
            "UnetLoaderGGUF",
            "TripleCLIPLoader",
            "VAELoader",
            "CLIPTextEncode",
            "ModelSamplingSD3",
            "EmptySD3LatentImage",
            "ConditioningZeroOut",
            "ConditioningSetTimestepRange",
            "ConditioningCombine",
            "ImageScale",
            "KSampler",
            "VAEDecode",
            "SaveImage",
        })
        payload["diffusion_models"] = [SD35_GGUF_FILE, "SD35/" + SD35_GGUF_FILE]
        payload["clip_models"] = [SD35_CLIP_L, SD35_CLIP_G, SD35_T5]
        payload["vaes"] = [SD35_VAE]
        payload["samplers"] = ["euler", "dpmpp_2m"]
        payload["schedulers"] = ["normal", "karras", "sgm_uniform"]
        return payload


class FailingComfyUIClient(FakeComfyUIClient):
    def generate_image(self, params, *, timeout_seconds=180, progress_callback=None):
        from services.comfyui.client import ComfyUIError

        raise ComfyUIError("ComfyUI 產圖失敗")


class FakePointsService:
    def __init__(self, balance=100, fail_spend=False):
        self.balance = int(balance)
        self.fail_spend = fail_spend
        self.spends = []

    def list_catalog(self):
        return [{
            "item_key": "comfyui_txt2img_basic",
            "item_name": "基礎生圖一次",
            "category": "comfyui",
            "currency_type": "points",
            "base_price": 5,
            "enabled": 1,
            "metadata": {},
        }]

    def get_wallet(self, user_id):
        return {"user_id": user_id, "points_balance": self.balance}

    def rc1_facade(self):
        return self

    def spend_service_fee(self, **kwargs):
        return self.spend_points(**kwargs)

    def spend_points(self, *, user_id, item_key, quantity=1, reference_type=None,
                     reference_id=None, idempotency_key=None, metadata=None, actor=None, override_amount=None):
        if self.fail_spend:
            raise ValueError("billing failed")
        amount = int(override_amount) if override_amount is not None else 5 * int(quantity or 1)
        if self.balance < amount:
            raise ValueError("insufficient balance")
        self.balance -= amount
        spend = {
            "user_id": user_id,
            "item_key": item_key,
            "quantity": int(quantity or 1),
            "reference_type": reference_type,
            "reference_id": reference_id,
            "metadata": metadata or {},
            "amount": amount,
        }
        self.spends.append(spend)
        return {
            "ok": True,
            "ledger": {"ledger_uuid": f"ledger-{len(self.spends)}", "amount": amount},
            "wallet": self.get_wallet(user_id),
            "item": {"base_price": 5},
        }


class _FakeHeaders(dict):
    def get_content_charset(self):
        return "utf-8"


class _FakeResponse:
    def __init__(self, *, url, body, headers=None, final_url=None):
        self._url = final_url or url
        self._buffer = io.BytesIO(body if isinstance(body, bytes) else body.encode("utf-8"))
        self.headers = _FakeHeaders(headers or {})

    def read(self, amt=-1):
        return self._buffer.read(amt)

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _json_resp(payload, status=200):
    return make_response(jsonify(payload), status)


def _passthrough(fn):
    return fn


def _actor():
    return {
        "id": 1,
        "username": "alice",
        "role": "user",
        "member_level": "trusted",
        "effective_level": "trusted",
        "sanction_status": "none",
    }


def _await_comfyui_job(client, started_response, *, expected_status="completed", attempts=100):
    assert started_response.status_code == 200, started_response.get_json()
    started_body = started_response.get_json()
    assert started_body["ok"] is True
    assert started_body["async"] is True
    job_id = started_body["job"]["job_id"]
    final_body = None
    for _ in range(attempts):
        polled = client.get(f"/api/comfyui/jobs/{job_id}")
        assert polled.status_code == 200
        final_body = polled.get_json()
        status = final_body["job"]["status"]
        if status == expected_status:
            return final_body["job"]
        if status in {"completed", "error"} and status != expected_status:
            pytest.fail(f"ComfyUI job ended with {status}: {final_body['job'].get('error')}")
        time.sleep(0.05)
    pytest.fail(f"ComfyUI job did not reach {expected_status}: {final_body}")


def _await_comfyui_result(client, started_response, *, attempts=100):
    job = _await_comfyui_job(client, started_response, expected_status="completed", attempts=attempts)
    assert job["result"]
    return job["result"]


def _generate_preview(client):
    generated = client.post(
        "/api/comfyui/generate",
        json={
            "model": "dream.safetensors",
            "prompt": "a quiet test image",
            "width": 512,
            "height": 512,
            "steps": 12,
            "cfg": 6.5,
            "sampler_name": "euler",
            "scheduler": "normal",
            "seed": 123,
            "batch_size": 1,
            "confirm_billing": True,
        },
    )
    return _await_comfyui_result(client, generated)["image"]


class OfflineComfyUIClient:
    base_url = "http://fake-offline"

    def health_check(self, *, timeout=3):
        from services.comfyui.client import ComfyUIError

        raise ComfyUIError("ComfyUI 連線失敗：refused")


class RecoveringComfyUIClient:
    def __init__(self, state):
        self.state = state
        self.base_url = "http://localhost:8192"

    def health_check(self, *, timeout=3):
        from services.comfyui.client import ComfyUIError

        if not self.state.get("ready"):
            raise ComfyUIError("ComfyUI 連線失敗：refused")
        return {"ok": True, "system": {"os": "test"}}


class UrlEchoComfyUIClient:
    def __init__(self, base_url):
        self.base_url = base_url

    def health_check(self, *, timeout=3):
        return {"ok": True, "system": {"backend": self.base_url}}


class TrackingBackendClient:
    def __init__(self, base_url, *, model_name, filename):
        self.base_url = base_url
        self.model_name = model_name
        self.filename = filename
        self.generate_calls = []
        self.fetch_calls = []
        self.discard_calls = []
        self.interrupted = 0

    def health_check(self, *, timeout=3):
        return {"ok": True, "system": {"backend": self.base_url}}

    def get_models(self):
        return [self.model_name]

    def get_loras(self):
        return []

    def get_vaes(self):
        return ["backend.vae.safetensors"]

    def get_embeddings(self):
        return ["backend-embed.pt"]

    def get_sampler_options(self):
        return {"samplers": ["euler"], "schedulers": ["normal"]}

    def generate_image(self, params, *, timeout_seconds=180, progress_callback=None):
        self.generate_calls.append({"params": dict(params), "timeout_seconds": timeout_seconds})
        if progress_callback:
            progress_callback({
                "phase": "running",
                "percent": 50,
                "current": 1,
                "max": 1,
                "detail": f"{self.base_url} generating",
                "queue_remaining": 0,
            })
        image_ref = {"filename": self.filename, "subfolder": "", "type": "output"}
        image_data = f"generated:{self.base_url}".encode("utf-8")
        return {
            "prompt_id": f"prompt:{self.base_url}",
            "image_ref": image_ref,
            "mime_type": "image/png",
            "data": image_data,
            "images": [{
                "image_ref": image_ref,
                "mime_type": "image/png",
                "data": image_data,
            }],
        }

    def fetch_image(self, image_ref):
        self.fetch_calls.append(dict(image_ref))
        return ComfyUIImage(
            filename=image_ref.get("filename") or self.filename,
            subfolder=image_ref.get("subfolder") or "",
            type=image_ref.get("type") or "output",
            mime_type="image/png",
            data=f"fetched:{self.base_url}".encode("utf-8"),
        )

    def discard_image(self, image_ref, *, prompt_id=None, **kwargs):
        self.discard_calls.append({"image_ref": dict(image_ref), "prompt_id": prompt_id, **kwargs})
        return {"file_deleted": True, "file_missing": False, "file_delete_supported": True, "history_deleted": bool(prompt_id)}

    def interrupt(self):
        self.interrupted += 1
        return {"interrupted": True}


class LocalLoraMetadataClient(FakeComfyUIClient):
    def get_loras(self):
        return ["fancy_v2.safetensors"]


class MissingControlnetModelClient(FakeComfyUIClient):
    def get_capabilities(self):
        payload = super().get_capabilities()
        payload["controlnet_types"]["canny"]["available"] = False
        payload["controlnet_types"]["canny"]["matching_models"] = []
        return payload


class SubfolderModelOptionClient(FakeComfyUIClient):
    def get_loras(self):
        return ["QWEN/Qwen-Image-Lightning-4steps-V1.0.safetensors"]

    def get_vaes(self):
        return ["SDXL/sdxl_vae.safetensors"]

    def get_capabilities(self):
        payload = super().get_capabilities()
        payload["loras"] = self.get_loras()
        payload["vaes"] = self.get_vaes()
        payload["upscale_models"] = ["ESRGAN/OmniSR_X4_DIV2K.safetensors"]
        payload["controlnet_models"] = ["SD35/sd3.5_large_controlnet_canny.safetensors"]
        payload["controlnet_types"]["canny"]["available"] = True
        payload["controlnet_types"]["canny"]["matching_models"] = [
            "SD35/sd3.5_large_controlnet_canny.safetensors"
        ]
        return payload


class MissingWorkflowNodeClient(FakeComfyUIClient):
    def get_capabilities(self):
        payload = super().get_capabilities()
        payload["available_nodes"] = [node for node in payload["available_nodes"] if node != "VAEEncodeForInpaint"]
        return payload


class MissingWorkflowCheckpointClient(FakeComfyUIClient):
    def get_models(self):
        return ["photo.ckpt"]


def _write_lora_sidecar(base_dir, filename, *, base_model="", trained_words=None, extra=None):
    sidecar = Path(base_dir) / "models" / "loras" / f"{filename}.civitai.json"
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": "civitai",
        "base_model": base_model,
        "trained_words": list(trained_words or []),
    }
    if extra:
        payload.update(extra)
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return sidecar


def _init_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            role TEXT NOT NULL
        );
        INSERT INTO users (id, username, role) VALUES (1, 'alice', 'user');
        """
    )
    ensure_member_level_rules_schema(conn)
    ensure_upload_security_schema(conn)
    ensure_cloud_drive_attachment_schema(conn)
    ensure_storage_album_schema(conn)
    update_cloud_drive_security_policy(conn, {"scanner_enabled": False})
    conn.commit()
    conn.close()


def _build_app(db_path, storage_root, settings=None, comfyui_client=None, actor=None, points_service=None, extra_deps=None):
    app = Flask(__name__)
    app.testing = True

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    deps = {
        "STORAGE_DIR": str(storage_root),
        "audit": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": actor or _actor,
        "get_db": get_db,
        "get_system_settings": lambda: {"feature_comfyui_enabled": True, **(settings or {})},
        "get_member_level_rule": lambda conn, level: {
            "can_upload_attachment": True,
            "attachment_quota_mb": 10,
            "max_attachment_size_mb": 10,
            "upload_rate_limit_per_day": 10,
        },
        "get_ua": lambda: "test-agent",
        "json_resp": _json_resp,
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "comfyui_client": comfyui_client or FakeComfyUIClient(),
        "points_service": points_service or FakePointsService(),
    }
    if extra_deps:
        deps.update(extra_deps)
    register_comfyui_routes(app, deps)
    return app


def _import_workflow_preset(client, workflow, *, title="Workflow Preset", description="", visibility="private", default_params=None):
    payload = {
        "title": title,
        "description": description,
        "visibility": visibility,
        "workflow_json": workflow,
    }
    if default_params is not None:
        payload["default_params"] = default_params
    response = client.post("/api/comfyui/workflows/import", json=payload)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["preset"]


def _write_runtime_workflow_bundle(runtime_dir, bundle_id, *, name=None, description=""):
    workflow = FakeComfyUIClient().build_generation_workflow({
        "generation_mode": "txt2img",
        "model": "dream.safetensors",
        "prompt": f"prompt:{bundle_id}",
        "negative_prompt": "bad",
        "width": 512,
        "height": 512,
        "steps": 20,
        "cfg": 7.5,
        "sampler_name": "euler",
        "scheduler": "normal",
        "seed": 42,
        "batch_size": 1,
    })
    bundle_dir = Path(runtime_dir) / bundle_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "workflow.json").write_text(
        json.dumps(workflow, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (bundle_dir / "manifest.json").write_text(
        json.dumps(
            {
                "id": bundle_id,
                "name": name or bundle_id,
                "description": description,
                "schema_version": 1,
                "workflow_file": "workflow.json",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return bundle_dir


def test_comfyui_workflow_list_syncs_only_builtin_runtime_bundles_as_official_presets(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    runtime_dir = tmp_path / "runtime" / "comfyui"
    runtime_dir.mkdir(parents=True)
    _write_runtime_workflow_bundle(
        runtime_dir,
        "origin_sdxl_txt2img",
        name="SDXL Text-to-Image",
        description="builtin bundle",
    )
    _write_runtime_workflow_bundle(
        runtime_dir,
        "imported_custom_demo",
        name="Imported Demo",
        description="should stay non-official",
    )

    with patch("routes.comfyui.runtime_comfyui_dir", lambda *args, **kwargs: runtime_dir):
        client = _build_app(db_path, storage_root).test_client()
        first = client.get("/api/comfyui/workflows")
        assert first.status_code == 200
        first_body = first.get_json()
        assert [item["system_bundle_id"] for item in first_body["official_presets"]] == ["origin_sdxl_txt2img"]
        assert first_body["official_presets"][0]["title"] == "SDXL Text-to-Image"
        assert first_body["official_presets"][0]["manifest_summary"]["available"] is True
        assert first_body["official_presets"][0]["manifest_summary"]["id"] == "origin_sdxl_txt2img"

        second = client.get("/api/comfyui/workflows")
        assert second.status_code == 200
        second_body = second.get_json()
        assert [item["system_bundle_id"] for item in second_body["official_presets"]] == ["origin_sdxl_txt2img"]
        detail = client.get(f"/api/comfyui/workflows/{second_body['official_presets'][0]['id']}")
        assert detail.status_code == 200
        assert detail.get_json()["preset"]["manifest_json"]["id"] == "origin_sdxl_txt2img"

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT system_bundle_id, is_official FROM comfyui_workflow_presets ORDER BY id ASC"
    ).fetchall()
    conn.close()
    assert rows == [("origin_sdxl_txt2img", 1)]


def test_comfyui_models_and_generate_routes(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    _write_lora_sidecar(comfy_base, "detail.safetensors", base_model="SDXL")
    _write_lora_sidecar(comfy_base, "anime-style.safetensors", base_model="Flux")
    client = _build_app(
        db_path,
        storage_root,
        settings={"comfyui_base_dir": str(comfy_base)},
    ).test_client()

    models = client.get("/api/comfyui/models")
    assert models.status_code == 200
    assert models.get_json()["models"] == ["dream.safetensors", "photo.ckpt"]
    assert models.get_json()["loras"] == ["detail.safetensors", "anime-style.safetensors"]
    assert models.get_json()["lora_details"]["detail.safetensors"]["trained_words"] == []
    assert models.get_json()["lora_details"]["detail.safetensors"]["base_model"] == "SDXL"
    assert models.get_json()["lora_details"]["detail.safetensors"]["supported"] is True
    assert models.get_json()["lora_details"]["anime-style.safetensors"]["base_model"] == "Flux"
    assert models.get_json()["lora_details"]["anime-style.safetensors"]["supported"] is False
    assert models.get_json()["vaes"] == ["sdxl_vae.safetensors", "anime_vae.pt"]
    assert models.get_json()["embeddings"] == ["badhandv4.pt", "easynegative.safetensors"]
    assert {item["key"] for item in models.get_json()["generation_modes"]} >= {"txt2img", "img2img", "inpaint", "outpaint", "upscale", "t2v", "i2v", "v2v", "t2s", "t2sv"}
    assert {item["key"] for item in models.get_json()["model_families"]} >= {"flux", "wan"}
    assert models.get_json()["controlnet_types"]["canny"]["available"] is True
    assert "control_v11p_sd15_canny.safetensors" in models.get_json()["controlnet_models"]
    assert "4x-UltraSharp.pth" in models.get_json()["upscale_models"]
    assert models.get_json()["max_batch_size"] == 1
    assert models.get_json()["default_width"] == 1024
    assert models.get_json()["default_height"] == 1024

    status = client.get("/api/comfyui/status")
    assert status.status_code == 200
    assert status.get_json()["available"] is True
    assert status.get_json()["max_batch_size"] == 1
    assert status.get_json()["default_width"] == 1024
    assert status.get_json()["default_height"] == 1024

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "model": "dream.safetensors",
            "prompt": "a quiet test image",
            "width": 512,
            "height": 512,
            "steps": 12,
            "cfg": 6.5,
            "sampler_name": "euler",
            "scheduler": "normal",
            "vae": "sdxl_vae.safetensors",
            "seed": 123,
            "batch_size": 3,
            "loras": [{"name": "detail.safetensors", "strength_model": 0.8, "strength_clip": 0.7}],
            "confirm_billing": True,
        },
    )
    assert generated.status_code == 200
    job_id = generated.get_json()["job"]["job_id"]
    body = _await_comfyui_result(client, generated)
    assert body["image"]["prompt_id"] == "prompt-1"
    assert "data_url" not in body["image"]
    assert body["image"]["seed"] == 123
    assert body["image"]["batch_size"] == 1
    assert len(body["images"]) == 1
    assert body["images"][0]["image_ref"]["filename"] == "hackme_web_00001_.png"
    preview = client.post("/api/comfyui/image-preview", json={"image_ref": body["image"]["image_ref"]})
    assert preview.status_code == 200
    assert preview.get_json()["image"]["data_url"].startswith("data:image/png;base64,")
    media_preview = client.post("/api/comfyui/media-preview", json={"job_id": job_id, "file_ref": body["image"]["image_ref"]})
    assert media_preview.status_code == 200, media_preview.get_json()
    assert media_preview.get_json()["media"]["data_url"].startswith("data:image/png;base64,")
    conn = sqlite3.connect(db_path)
    try:
        stored_result = conn.execute(
            "SELECT result_json FROM comfyui_generation_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert '"data_url"' not in stored_result
    assert len(stored_result) < 5000
    assert FakeComfyUIClient.last_timeout_seconds == 0
    assert FakeComfyUIClient.last_params["loras"] == [{"name": "detail.safetensors", "strength_model": 0.8, "strength_clip": 0.7}]
    assert FakeComfyUIClient.last_params["vae"] == "sdxl_vae.safetensors"


def test_comfyui_img2img_controlnet_generate_uploads_assets_and_records_history(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    FakeComfyUIClient.uploaded_images = []
    client = _build_app(db_path, storage_root).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        data={
            "generation_mode": "img2img",
            "model": "dream.safetensors",
            "diffusers_model_repo": "black-forest-labs/FLUX.1-schnell",
            "diffusers_model_variant": "fp16",
            "prompt": "repaint the scene",
            "negative_prompt": "blurry",
            "width": "512",
            "height": "512",
            "steps": "18",
            "cfg": "7",
            "sampler_name": "euler",
            "scheduler": "normal",
            "seed": "123",
            "batch_size": "1",
            "ui_batch_size": "1",
            "run_count": "3",
            "seed_after_generate": "increment",
            "confirm_billing": "true",
            "denoise_strength": "0.55",
            "controlnet_enabled": "true",
            "controlnet_type": "canny",
            "controlnet_model": "control_v11p_sd15_canny.safetensors",
            "controlnet_preprocessor": "CannyEdgePreprocessor",
            "control_strength": "0.9",
            "control_start": "0.1",
            "control_end": "0.8",
            "source_image": (io.BytesIO(b"source-bytes"), "source.png", "image/png"),
            "control_image": (io.BytesIO(b"control-bytes"), "control.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert generated.status_code == 200
    body = _await_comfyui_result(client, generated)
    assert body["history_id"] > 0
    assert FakeComfyUIClient.last_params["generation_mode"] == "img2img"
    assert FakeComfyUIClient.last_params["source_image_ref"]["filename"] == "source.png"
    assert FakeComfyUIClient.last_params["controlnet"]["image_ref"]["filename"] == "control.png"
    assert FakeComfyUIClient.last_params["controlnet"]["type"] == "canny"
    assert len(FakeComfyUIClient.uploaded_images) == 2

    history = client.get("/api/comfyui/history")
    assert history.status_code == 200
    history_body = history.get_json()
    history_item = history_body["history"][0]
    assert history_item["generation_mode"] == "img2img"
    assert history_item["controlnet"]["type"] == "canny"
    assert history_item["input_assets"]["source_image_ref"]["filename"] == "source.png"
    assert history_item["payload"]["diffusers_model_repo"] == "black-forest-labs/FLUX.1-schnell"
    assert history_item["payload"]["diffusers_model_variant"] == "fp16"
    assert history_item["payload"]["ui_batch_size"] == 1
    assert history_item["payload"]["run_count"] == 3
    assert history_item["payload"]["seed_after_generate"] == "increment"
    assert history_item["payload"]["source_image_ref"]["filename"] == "source.png"
    assert history_item["payload"]["controlnet"]["image_ref"]["filename"] == "control.png"

    FakeComfyUIClient.last_params = {}
    rerun = client.post(f"/api/comfyui/history/{body['history_id']}/rerun", json={})
    assert rerun.status_code == 200
    _await_comfyui_result(client, rerun)
    assert FakeComfyUIClient.last_params["generation_mode"] == "img2img"
    assert FakeComfyUIClient.last_params["diffusers_model_repo"] == "black-forest-labs/FLUX.1-schnell"
    assert FakeComfyUIClient.last_params["diffusers_model_variant"] == "fp16"
    assert FakeComfyUIClient.last_params["ui_batch_size"] == 1
    assert FakeComfyUIClient.last_params["run_count"] == 3
    assert FakeComfyUIClient.last_params["seed_after_generate"] == "increment"
    assert FakeComfyUIClient.last_params["source_image_ref"]["filename"] == "source.png"
    assert FakeComfyUIClient.last_params["controlnet"]["image_ref"]["filename"] == "control.png"


def test_comfyui_generate_rejects_controlnet_strength_out_of_range(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": "dream.safetensors",
            "prompt": "a robot portrait",
            "controlnet_enabled": True,
            "controlnet_type": "canny",
            "control_strength": 2.5,
        },
    )
    assert generated.status_code == 400
    assert "Control strength" in generated.get_json()["msg"]


def test_comfyui_generate_rejects_when_controlnet_model_missing(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root, comfyui_client=MissingControlnetModelClient()).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        data={
            "generation_mode": "img2img",
            "model": "dream.safetensors",
            "prompt": "repaint the scene",
            "confirm_billing": "true",
            "source_image": (io.BytesIO(b"source-bytes"), "source.png", "image/png"),
            "control_image": (io.BytesIO(b"control-bytes"), "control.png", "image/png"),
            "controlnet_enabled": "true",
            "controlnet_type": "canny",
        },
        content_type="multipart/form-data",
    )
    assert generated.status_code == 409
    assert "缺少對應" in generated.get_json()["msg"]


def test_comfyui_generate_accepts_subfolder_lora_and_vae_options(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root, comfyui_client=SubfolderModelOptionClient()).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": "dream.safetensors",
            "prompt": "subfolder model option test",
            "confirm_billing": True,
            "vae": "SDXL\\sdxl_vae.safetensors",
            "loras": [
                {"name": "QWEN\\Qwen-Image-Lightning-4steps-V1.0.safetensors", "strength_model": 1}
            ],
        },
    )

    assert generated.status_code == 200
    _await_comfyui_result(client, generated)
    assert FakeComfyUIClient.last_params["vae"] == "SDXL/sdxl_vae.safetensors"
    assert FakeComfyUIClient.last_params["loras"][0]["name"] == "QWEN/Qwen-Image-Lightning-4steps-V1.0.safetensors"


def test_comfyui_generate_accepts_subfolder_controlnet_model_option(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root, comfyui_client=SubfolderModelOptionClient()).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        data={
            "generation_mode": "img2img",
            "model": "dream.safetensors",
            "prompt": "controlnet subfolder path",
            "confirm_billing": "true",
            "source_image": (io.BytesIO(b"source-bytes"), "source.png", "image/png"),
            "control_image": (io.BytesIO(b"control-bytes"), "control.png", "image/png"),
            "controlnet_enabled": "true",
            "controlnet_type": "canny",
            "controlnet_model": "SD35\\sd3.5_large_controlnet_canny.safetensors",
        },
        content_type="multipart/form-data",
    )

    assert generated.status_code == 200
    _await_comfyui_result(client, generated)
    assert FakeComfyUIClient.last_params["controlnet"]["model_name"] == "SD35/sd3.5_large_controlnet_canny.safetensors"


def test_comfyui_generate_accepts_subfolder_upscale_model_option(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root, comfyui_client=SubfolderModelOptionClient()).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        data={
            "generation_mode": "upscale",
            "confirm_billing": "true",
            "source_image": (io.BytesIO(b"source-bytes"), "source.png", "image/png"),
            "upscale_model": "ESRGAN\\OmniSR_X4_DIV2K.safetensors",
        },
        content_type="multipart/form-data",
    )

    assert generated.status_code == 200
    _await_comfyui_result(client, generated)
    assert FakeComfyUIClient.last_params["upscale_model"] == "ESRGAN/OmniSR_X4_DIV2K.safetensors"


def test_comfyui_generate_rejects_when_workflow_node_missing(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root, comfyui_client=MissingWorkflowNodeClient()).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        data={
            "generation_mode": "inpaint",
            "model": "dream.safetensors",
            "prompt": "repair the face",
            "confirm_billing": "true",
            "source_image": (io.BytesIO(b"source-bytes"), "source.png", "image/png"),
            "mask_image": (io.BytesIO(b"mask-bytes"), "mask.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert generated.status_code == 409
    assert "workflow node" in generated.get_json()["msg"]


def test_comfyui_generate_rejects_invalid_control_image_format(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        data={
            "generation_mode": "img2img",
            "model": "dream.safetensors",
            "prompt": "repaint the scene",
            "source_image": (io.BytesIO(b"source-bytes"), "source.png", "image/png"),
            "control_image": (io.BytesIO(b"bad-bytes"), "control.txt", "text/plain"),
            "controlnet_enabled": "true",
            "controlnet_type": "canny",
        },
        content_type="multipart/form-data",
    )
    assert generated.status_code == 400
    assert "控制圖 只支援 PNG / JPG / WEBP" in generated.get_json()["msg"]


def test_comfyui_history_rerun_reuses_saved_assets(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    FakeComfyUIClient.uploaded_images = []
    client = _build_app(db_path, storage_root).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        data={
            "generation_mode": "upscale",
            "model": "dream.safetensors",
            "prompt": "",
            "upscale_model": "4x-UltraSharp.pth",
            "confirm_billing": "true",
            "source_image": (io.BytesIO(b"source-bytes"), "source.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert generated.status_code == 200
    history_id = _await_comfyui_result(client, generated)["history_id"]
    FakeComfyUIClient.uploaded_images = []

    rerun = client.post(f"/api/comfyui/history/{history_id}/rerun", json={})
    assert rerun.status_code == 200
    job_id = rerun.get_json()["job"]["job_id"]

    polled = client.get(f"/api/comfyui/jobs/{job_id}")
    assert polled.status_code == 200
    body = polled.get_json()
    for _ in range(100):
      if body["job"]["status"] == "completed":
        break
      time.sleep(0.05)
      body = client.get(f"/api/comfyui/jobs/{job_id}").get_json()
    assert body["job"]["status"] == "completed"
    rerun_history_id = body["job"]["result"]["history_id"]
    assert rerun_history_id > 0
    assert rerun_history_id != history_id
    assert FakeComfyUIClient.uploaded_images == []
    assert FakeComfyUIClient.last_params["generation_mode"] == "upscale"
    assert FakeComfyUIClient.last_params["source_image_ref"]["filename"] == "source.png"
    history = client.get("/api/comfyui/history")
    assert history.status_code == 200
    history_ids = [item["id"] for item in history.get_json()["history"]]
    assert history_ids.index(rerun_history_id) < history_ids.index(history_id)


def test_comfyui_workflow_import_rejects_bad_json_and_unsafe_paths(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    bad_json = client.post(
        "/api/comfyui/workflows/import",
        json={"title": "broken", "workflow_json": "{not-json"},
    )
    assert bad_json.status_code == 400
    assert "workflow JSON 格式不正確" in bad_json.get_json()["msg"]
    assert bad_json.get_json()["stage"] == "schema_validation"

    unsafe_path = client.post(
        "/api/comfyui/workflows/import",
        json={
            "title": "unsafe",
            "workflow_json": {
                "1": {
                    "class_type": "LoadImage",
                    "inputs": {"image": "/tmp/evil.png", "upload": "image"},
                }
            },
        },
    )
    assert unsafe_path.status_code == 400
    assert "絕對路徑" in unsafe_path.get_json()["msg"]
    assert unsafe_path.get_json()["stage"] == "sanitize"

    unsafe_url = client.post(
        "/api/comfyui/workflows/import",
        json={
            "title": "unsafe-url",
            "workflow_json": {
                "1": {
                    "class_type": "LoadImage",
                    "inputs": {"image": "https://evil.example/payload.png", "upload": "image"},
                }
            },
        },
    )
    assert unsafe_url.status_code == 400
    assert "外部 URL" in unsafe_url.get_json()["msg"]
    assert unsafe_url.get_json()["stage"] == "sanitize"


def test_comfyui_workflow_import_rejects_too_many_nodes_and_deep_nesting(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    too_many_nodes = {
        str(index): {
            "class_type": "LoadImage",
            "inputs": {"image": f"image-{index}.png", "upload": "image"},
        }
        for index in range(201)
    }
    too_many = client.post(
        "/api/comfyui/workflows/import",
        json={"title": "too-many", "workflow_json": too_many_nodes},
    )
    assert too_many.status_code == 400
    assert "node 數量過多" in too_many.get_json()["msg"]

    nested_value = "leaf"
    for _ in range(12):
        nested_value = [nested_value]
    too_deep = client.post(
        "/api/comfyui/workflows/import",
        json={
            "title": "too-deep",
            "workflow_json": {
                "1": {
                    "class_type": "LoadImage",
                    "inputs": {"image": nested_value, "upload": "image"},
                }
            },
        },
    )
    assert too_deep.status_code == 400
    assert "巢狀層級過深" in too_deep.get_json()["msg"]


def test_comfyui_private_workflow_preset_cannot_be_read_by_other_user(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO users (id, username, role) VALUES (2, 'bob', 'user')")
    conn.commit()
    conn.close()

    alice_actor = lambda: {"id": 1, "username": "alice", "role": "user"}
    bob_actor = lambda: {"id": 2, "username": "bob", "role": "user"}
    owner_client = _build_app(db_path, storage_root, actor=alice_actor).test_client()
    viewer_client = _build_app(db_path, storage_root, actor=bob_actor).test_client()

    workflow = FakeComfyUIClient().build_generation_workflow({
        "generation_mode": "txt2img",
        "model": "dream.safetensors",
        "prompt": "owner only",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 12,
        "cfg": 7,
        "seed": 123,
        "batch_size": 1,
        "sampler_name": "euler",
        "scheduler": "normal",
        "filename_prefix": "preset",
    })
    preset = _import_workflow_preset(owner_client, workflow, title="Private Flow", visibility="private")

    forbidden = viewer_client.get(f"/api/comfyui/workflows/{preset['id']}")
    assert forbidden.status_code == 403
    assert "沒有權限" in forbidden.get_json()["msg"]


def test_comfyui_workflow_layout_metadata_versions_export_and_reimport(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    workflow = FakeComfyUIClient().build_generation_workflow({
        "generation_mode": "txt2img",
        "model": "dream.safetensors",
        "prompt": "layout builder",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 14,
        "cfg": 6,
        "seed": 321,
        "batch_size": 1,
        "sampler_name": "euler",
        "scheduler": "normal",
        "filename_prefix": "layout",
    })
    imported = client.post(
        "/api/comfyui/workflow-layouts",
        json={
            "title": "Layout Builder Flow",
            "description": "custom layout",
            "purpose": "txt2img",
            "project_version": "pytest-project",
            "comfyui_version": "pytest-comfy",
            "workflow_schema_version": "1",
            "layout_json": {
                "layout_schema_version": "1",
                "node_order": ["1", "2", "3"],
                "node_positions": {"3": {"x": 120, "y": 240}},
                "field_overrides": {"steps": {"label": "Steps"}},
            },
            "workflow_json": workflow,
            "is_default": True,
        },
    )
    assert imported.status_code == 200, imported.get_json()
    preset = imported.get_json()["preset"]
    assert preset["purpose"] == "txt2img"
    assert preset["project_version"] == "pytest-project"
    assert preset["comfyui_version"] == "pytest-comfy"
    assert preset["workflow_schema_version"] == "1"
    assert preset["layout_json"]["node_positions"]["3"]["x"] == 120
    assert preset["is_default"] is True

    updated = client.put(
        f"/api/comfyui/workflow-layouts/{preset['id']}",
        json={
            "title": "Layout Builder Flow v2",
            "workflow_json": workflow,
            "layout_json": {"layout_schema_version": "1", "node_order": ["3", "2", "1"]},
        },
    )
    assert updated.status_code == 200, updated.get_json()

    detail = client.get(f"/api/comfyui/workflow-layouts/{preset['id']}")
    assert detail.status_code == 200, detail.get_json()
    detail_json = detail.get_json()["preset"]
    assert detail_json["layout_json"]["node_order"] == ["3", "2", "1"]
    assert len(detail_json["layout_versions"]) >= 2

    exported = client.post(f"/api/comfyui/workflow-layouts/{preset['id']}/export", json={})
    assert exported.status_code == 200, exported.get_json()
    exported_json = exported.get_json()
    assert exported_json["raw_workflow_json"]
    assert exported_json["workflow_preset_json"]["project_version"] == "pytest-project"
    assert exported_json["workflow_preset_json"]["layout_json"]["node_order"] == ["3", "2", "1"]
    assert "workflow_preset_text" in exported_json

    reimported = client.post(
        "/api/comfyui/workflow-layouts/import",
        json={"workflow_json": exported_json["workflow_preset_json"], "title": "Reimported Layout"},
    )
    assert reimported.status_code == 200, reimported.get_json()
    assert reimported.get_json()["preset"]["title"] == "Reimported Layout"


def test_comfyui_export_current_and_run_workflow_preset_preserve_parameters(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    exported = client.post(
        "/api/comfyui/workflows/export-current",
        json={
            "generation_mode": "txt2img",
            "model": "dream.safetensors",
            "prompt": "workflow export prompt",
            "negative_prompt": "workflow export negative",
            "width": 640,
            "height": 768,
            "steps": 16,
            "cfg": 5.5,
            "seed": 424242,
            "batch_size": 1,
            "sampler_name": "euler",
            "scheduler": "normal",
            "vae": "sdxl_vae.safetensors",
        },
    )
    assert exported.status_code == 200, exported.get_json()
    exported_json = exported.get_json()
    assert exported_json["ok"] is True
    assert "/tmp/" not in exported_json["workflow_text"]
    assert "https://" not in exported_json["workflow_text"]
    assert exported_json["default_params"]["seed"] == 424242
    assert exported_json["default_params"]["cfg"] == 5.5

    preset = _import_workflow_preset(
        client,
        exported_json["workflow_json"],
        title="Exported Flow",
        default_params=exported_json["default_params"],
    )
    exported_preset = client.post(f"/api/comfyui/workflows/{preset['id']}/export", json={})
    assert exported_preset.status_code == 200, exported_preset.get_json()
    preset_export_json = exported_preset.get_json()
    assert "/tmp/" not in preset_export_json["workflow_text"]
    assert "https://" not in preset_export_json["workflow_text"]

    run = client.post(f"/api/comfyui/workflows/{preset['id']}/run", json={})
    assert run.status_code == 200, run.get_json()
    job_id = run.get_json()["job"]["job_id"]

    body = client.get(f"/api/comfyui/jobs/{job_id}").get_json()
    for _ in range(100):
        if body["job"]["status"] == "completed":
            break
        time.sleep(0.1)
        body = client.get(f"/api/comfyui/jobs/{job_id}").get_json()
    assert body["job"]["status"] == "completed"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    run_row = conn.execute(
        "SELECT params_json, workflow_json, status FROM comfyui_workflow_runs WHERE preset_id=? ORDER BY id DESC LIMIT 1",
        (int(preset["id"]),),
    ).fetchone()
    conn.close()
    assert run_row is not None
    saved_params = json.loads(run_row["params_json"])
    assert saved_params["seed"] == 424242
    assert saved_params["cfg"] == 5.5
    assert saved_params["steps"] == 16
    assert saved_params["prompt"] == "workflow export prompt"
    assert saved_params["negative_prompt"] == "workflow export negative"
    assert run_row["status"] == "completed"
    assert FakeComfyUIClient.last_workflow["3"]["inputs"]["seed"] == 424242
    assert FakeComfyUIClient.last_workflow["3"]["inputs"]["steps"] == 16
    assert FakeComfyUIClient.last_workflow["3"]["inputs"]["cfg"] == 5.5
    assert FakeComfyUIClient.last_wait_until_completed is True


def test_comfyui_workflow_audio_media_preview_can_play_generated_output(tmp_path):
    class AudioWorkflowClient(FakeComfyUIClient):
        def generate_from_workflow(
            self,
            workflow,
            *,
            timeout_seconds=180,
            expected_count=1,
            progress_callback=None,
            fetch_outputs=False,
            extra_data=None,
        ):
            FakeComfyUIClient.generated_count += 1
            FakeComfyUIClient.last_workflow = json.loads(json.dumps(workflow))
            if progress_callback:
                progress_callback({
                    "phase": "running",
                    "percent": 50,
                    "current": 1,
                    "max": 1,
                    "current_node": "audio",
                    "detail": "audio workflow 執行中",
                    "queue_remaining": 0,
                })
            file_ref = {"filename": "ace_step_preview.mp3", "subfolder": "audio", "type": "output"}
            return {
                "prompt_id": "audio-prompt-1",
                "image_ref": file_ref,
                "mime_type": "application/octet-stream",
                "data": b"",
                "images": [],
                "media": {
                    "audio": [{
                        "file_ref": file_ref,
                        "mime_type": "application/octet-stream",
                        "data": b"",
                        "size_bytes": 0,
                    }],
                },
            }

        def fetch_file(self, file_ref):
            assert file_ref == {"filename": "ace_step_preview.mp3", "subfolder": "audio", "type": "output"}
            return ComfyUIImage(
                filename=file_ref["filename"],
                subfolder=file_ref["subfolder"],
                type=file_ref["type"],
                mime_type="application/octet-stream",
                data=b"ID3fake-mp3-bytes",
            )

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root, comfyui_client=AudioWorkflowClient()).test_client()
    exported = client.post(
        "/api/comfyui/workflows/export-current",
        json={
            "generation_mode": "txt2img",
            "model": "dream.safetensors",
            "prompt": "ace audio smoke",
            "negative_prompt": "",
            "width": 512,
            "height": 512,
            "steps": 8,
            "cfg": 4,
            "seed": 123,
            "batch_size": 1,
            "sampler_name": "euler",
            "scheduler": "normal",
        },
    )
    assert exported.status_code == 200, exported.get_json()
    preset = _import_workflow_preset(
        client,
        exported.get_json()["workflow_json"],
        title="Audio Output Flow",
        default_params=exported.get_json()["default_params"],
    )

    run = client.post(f"/api/comfyui/workflows/{preset['id']}/run", json={})
    assert run.status_code == 200, run.get_json()
    job_id = run.get_json()["job"]["job_id"]
    body = client.get(f"/api/comfyui/jobs/{job_id}").get_json()
    for _ in range(100):
        if body["job"]["status"] == "completed":
            break
        time.sleep(0.1)
        body = client.get(f"/api/comfyui/jobs/{job_id}").get_json()
    assert body["job"]["status"] == "completed"
    media = body["job"]["result"]["media"]
    assert media[0]["media_kind"] == "audio"
    assert "data_url" not in media[0]

    preview = client.post(
        "/api/comfyui/media-preview",
        json={"job_id": job_id, "file_ref": media[0]["file_ref"]},
    )
    assert preview.status_code == 200, preview.get_json()
    preview_json = preview.get_json()
    assert preview_json["media"]["mime_type"] == "audio/mpeg"
    assert preview_json["media"]["data_url"].startswith("data:audio/mpeg;base64,")
    assert preview_json["media"]["size_bytes"] == len(b"ID3fake-mp3-bytes")


def test_comfyui_workflow_video_media_preview_can_play_generated_output(tmp_path):
    class VideoWorkflowClient(FakeComfyUIClient):
        def generate_from_workflow(
            self,
            workflow,
            *,
            timeout_seconds=180,
            expected_count=1,
            progress_callback=None,
            fetch_outputs=False,
            extra_data=None,
        ):
            FakeComfyUIClient.generated_count += 1
            FakeComfyUIClient.last_workflow = json.loads(json.dumps(workflow))
            file_ref = {"filename": "ltx_preview.mp4", "subfolder": "video", "type": "output"}
            return {
                "prompt_id": "video-prompt-1",
                "image_ref": file_ref,
                "mime_type": "application/octet-stream",
                "data": b"",
                "images": [],
                "media": {
                    "videos": [{
                        "file_ref": file_ref,
                        "mime_type": "application/octet-stream",
                        "data": b"",
                        "size_bytes": 0,
                    }],
                },
            }

        def fetch_file(self, file_ref):
            assert file_ref == {"filename": "ltx_preview.mp4", "subfolder": "video", "type": "output"}
            return ComfyUIImage(
                filename=file_ref["filename"],
                subfolder=file_ref["subfolder"],
                type=file_ref["type"],
                mime_type="application/octet-stream",
                data=b"\x00\x00\x00\x18ftypmp42fake-mp4",
            )

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root, comfyui_client=VideoWorkflowClient()).test_client()
    exported = client.post(
        "/api/comfyui/workflows/export-current",
        json={
            "generation_mode": "txt2img",
            "model": "dream.safetensors",
            "prompt": "video media smoke",
            "negative_prompt": "",
            "width": 512,
            "height": 512,
            "steps": 8,
            "cfg": 4,
            "seed": 123,
            "batch_size": 1,
            "sampler_name": "euler",
            "scheduler": "normal",
        },
    )
    assert exported.status_code == 200, exported.get_json()
    preset = _import_workflow_preset(
        client,
        exported.get_json()["workflow_json"],
        title="Video Output Flow",
        default_params=exported.get_json()["default_params"],
    )

    run = client.post(f"/api/comfyui/workflows/{preset['id']}/run", json={})
    body = _await_comfyui_job(client, run)
    media = body["result"]["media"]
    assert body["result"]["images"] == []
    assert media[0]["media_kind"] == "video"

    preview = client.post(
        "/api/comfyui/media-preview",
        json={"job_id": body["job_id"], "file_ref": media[0]["file_ref"]},
    )
    assert preview.status_code == 200, preview.get_json()
    preview_json = preview.get_json()
    assert preview_json["media"]["mime_type"] == "video/mp4"
    assert preview_json["media"]["data_url"].startswith("data:video/mp4;base64,")
    assert preview_json["media"]["size_bytes"] == len(b"\x00\x00\x00\x18ftypmp42fake-mp4")


def test_comfyui_media_preview_uses_original_workflow_backend_url(tmp_path):
    class BackendAwareAudioClient(FakeComfyUIClient):
        def __init__(self, base_url):
            self.base_url = base_url
            self.fetch_calls = []

        def generate_from_workflow(
            self,
            workflow,
            *,
            timeout_seconds=180,
            expected_count=1,
            progress_callback=None,
            fetch_outputs=False,
            extra_data=None,
        ):
            file_ref = {"filename": "ace_backend_preview.mp3", "subfolder": "audio", "type": "output"}
            return {
                "prompt_id": f"audio:{self.base_url}",
                "image_ref": file_ref,
                "mime_type": "application/octet-stream",
                "data": b"",
                "images": [],
                "media": {
                    "audio": [{
                        "file_ref": file_ref,
                        "mime_type": "application/octet-stream",
                        "data": b"",
                        "size_bytes": 0,
                    }],
                },
            }

        def fetch_file(self, file_ref):
            self.fetch_calls.append(dict(file_ref))
            return ComfyUIImage(
                filename=file_ref["filename"],
                subfolder=file_ref.get("subfolder") or "",
                type=file_ref.get("type") or "output",
                mime_type="application/octet-stream",
                data=f"mp3:{self.base_url}".encode("utf-8"),
            )

    settings = {
        "comfyui_connection_mode": "remote",
        "comfyui_remote_api_url": "http://run-backend:8188",
    }
    clients = {}

    def factory(url):
        clients.setdefault(url, BackendAwareAudioClient(url))
        return clients[url]

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings=settings,
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": factory,
        },
    ).test_client()
    exported = client.post(
        "/api/comfyui/workflows/export-current",
        json={
            "generation_mode": "txt2img",
            "model": "dream.safetensors",
            "prompt": "backend media smoke",
            "negative_prompt": "",
            "width": 512,
            "height": 512,
            "steps": 8,
            "cfg": 4,
            "seed": 123,
            "batch_size": 1,
            "sampler_name": "euler",
            "scheduler": "normal",
        },
    )
    assert exported.status_code == 200, exported.get_json()
    preset = _import_workflow_preset(
        client,
        exported.get_json()["workflow_json"],
        title="Backend Audio Output Flow",
        default_params=exported.get_json()["default_params"],
    )

    run = client.post(f"/api/comfyui/workflows/{preset['id']}/run", json={})
    body = _await_comfyui_job(client, run)
    media = body["result"]["media"]
    assert body["result"]["backend_url"] == "http://run-backend:8188"

    settings["comfyui_remote_api_url"] = "http://other-backend:8188"
    preview = client.post(
        "/api/comfyui/media-preview",
        json={"job_id": body["job_id"], "file_ref": media[0]["file_ref"]},
    )

    assert preview.status_code == 200, preview.get_json()
    assert clients["http://run-backend:8188"].fetch_calls == [media[0]["file_ref"]]
    assert clients.get("http://other-backend:8188") is None or clients["http://other-backend:8188"].fetch_calls == []


def test_comfyui_workflow_run_expects_multiple_preview_outputs(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()
    workflow = {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "dream.safetensors"}},
        "48": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "photo.ckpt"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "same prompt", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "same negative", "clip": ["4", 1]}},
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
                "seed": 42,
                "steps": 12,
                "cfg": 6,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1,
            },
        },
        "17": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["48", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
                "seed": 42,
                "steps": 12,
                "cfg": 6,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1,
            },
        },
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "18": {"class_type": "VAEDecode", "inputs": {"samples": ["17", 0], "vae": ["48", 2]}},
        "50": {"class_type": "PreviewImage", "inputs": {"images": ["8", 0]}},
        "51": {"class_type": "PreviewImage", "inputs": {"images": ["18", 0]}},
    }
    preset = _import_workflow_preset(
        client,
        workflow,
        title="Compare Preview Outputs",
        default_params={
            "generation_mode": "txt2img",
            "model": "dream.safetensors",
            "prompt": "same prompt",
            "negative_prompt": "same negative",
            "width": 512,
            "height": 512,
            "steps": 12,
            "cfg": 6,
            "seed": 42,
            "batch_size": 1,
            "sampler_name": "euler",
            "scheduler": "normal",
        },
    )

    run = client.post(f"/api/comfyui/workflows/{preset['id']}/run", json={})
    assert run.status_code == 200, run.get_json()
    job_id = run.get_json()["job"]["job_id"]
    body = client.get(f"/api/comfyui/jobs/{job_id}").get_json()
    for _ in range(100):
        if body["job"]["status"] == "completed":
            break
        time.sleep(0.1)
        body = client.get(f"/api/comfyui/jobs/{job_id}").get_json()

    assert body["job"]["status"] == "completed"
    assert FakeComfyUIClient.last_expected_count == 2
    assert len(body["job"]["result"]["images"]) == 2


def test_comfyui_workflow_run_rejects_missing_dependencies(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    workflow = FakeComfyUIClient().build_generation_workflow({
        "generation_mode": "txt2img",
        "model": "dream.safetensors",
        "prompt": "dependency test",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 20,
        "cfg": 7,
        "seed": 7,
        "batch_size": 1,
        "sampler_name": "euler",
        "scheduler": "normal",
        "filename_prefix": "dependency",
    })

    missing_model_client = _build_app(db_path, storage_root, comfyui_client=MissingWorkflowCheckpointClient()).test_client()
    preset = _import_workflow_preset(missing_model_client, workflow, title="Needs Checkpoint")
    missing_model = missing_model_client.post(f"/api/comfyui/workflows/{preset['id']}/run", json={})
    assert missing_model.status_code == 409
    assert "缺少模型" in missing_model.get_json()["msg"]
    assert missing_model.get_json()["stage"] == "missing_model"

    inpaint_workflow = FakeComfyUIClient().build_inpaint_workflow({
        "generation_mode": "inpaint",
        "model": "dream.safetensors",
        "prompt": "fill the gap",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 20,
        "cfg": 7,
        "seed": 9,
        "batch_size": 1,
        "sampler_name": "euler",
        "scheduler": "normal",
        "source_image_ref": {"filename": "source.png", "subfolder": "", "type": "input"},
        "mask_image_ref": {"filename": "mask.png", "subfolder": "", "type": "input"},
        "filename_prefix": "inpaint",
    })
    missing_node_client = _build_app(db_path, storage_root, comfyui_client=MissingWorkflowNodeClient()).test_client()
    preset_inpaint = _import_workflow_preset(missing_node_client, inpaint_workflow, title="Needs Node")
    missing_node = missing_node_client.post(f"/api/comfyui/workflows/{preset_inpaint['id']}/run", json={})
    assert missing_node.status_code == 409
    assert "缺少 workflow node" in missing_node.get_json()["msg"]


def test_root_can_publish_official_workflow_preset_with_audit(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO users (id, username, role) VALUES (9, 'root', 'super_admin')")
    conn.execute("INSERT INTO users (id, username, role) VALUES (10, 'admin', 'manager')")
    conn.commit()
    conn.close()

    audit_events = []
    root_actor = lambda: {"id": 9, "username": "root", "role": "super_admin"}
    admin_actor = lambda: {"id": 10, "username": "admin", "role": "manager"}
    workflow = FakeComfyUIClient().build_generation_workflow({
        "generation_mode": "txt2img",
        "model": "dream.safetensors",
        "prompt": "official preset",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 20,
        "cfg": 7,
        "seed": 77,
        "batch_size": 1,
        "sampler_name": "euler",
        "scheduler": "normal",
        "filename_prefix": "official",
    })

    root_client = _build_app(
        db_path,
        storage_root,
        actor=root_actor,
        extra_deps={"audit": lambda *args, **kwargs: audit_events.append((args, kwargs))},
    ).test_client()
    preset = _import_workflow_preset(root_client, workflow, title="Root Preset", visibility="private")

    admin_client = _build_app(db_path, storage_root, actor=admin_actor).test_client()
    forbidden = admin_client.post(f"/api/admin/comfyui/workflows/{preset['id']}/publish-official", json={})
    assert forbidden.status_code == 403

    published = root_client.post(f"/api/admin/comfyui/workflows/{preset['id']}/publish-official", json={})
    assert published.status_code == 200, published.get_json()
    body = published.get_json()
    assert body["preset"]["is_official"] is True
    assert body["preset"]["visibility"] == "public"
    assert any(args and args[0] == "COMFYUI_WORKFLOW_PUBLISH_OFFICIAL" for args, _kwargs in audit_events)


def test_comfyui_official_workflow_media_preview_serves_asset(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    media_dir = tmp_path / "official-media"
    media_dir.mkdir()
    (media_dir / "sample.png").write_bytes(b"official-sample-png")
    monkeypatch.setattr(comfyui_workflow_routes, "_OFFICIAL_TEMPLATE_MEDIA_DIR", media_dir)

    client = _build_app(db_path, storage_root).test_client()
    response = client.get("/api/comfyui/workflows/official-media/sample.png")

    assert response.status_code == 200
    assert response.data == b"official-sample-png"
    assert response.content_type.startswith("image/png")
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_comfyui_image_preview_returns_uploaded_asset_preview(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        data={
            "generation_mode": "img2img",
            "model": "dream.safetensors",
            "prompt": "repaint the scene",
            "confirm_billing": "true",
            "source_image": (io.BytesIO(b"source-bytes"), "source.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert generated.status_code == 200
    _await_comfyui_result(client, generated)

    history = client.get("/api/comfyui/history").get_json()["history"][0]
    source_ref = history["input_assets"]["source_image_ref"]
    preview = client.post("/api/comfyui/image-preview", json={"image_ref": source_ref})
    assert preview.status_code == 200
    image = preview.get_json()["image"]
    assert image["data_url"].startswith("data:image/png;base64,")
    assert image["image_ref"]["filename"] == "source.png"


def test_comfyui_input_image_candidates_import_history_and_drive_images(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    FakeComfyUIClient.uploaded_images = []
    client = _build_app(db_path, storage_root).test_client()

    preview = _generate_preview(client)
    actor = _actor()
    rel_path = "users/1/manual/drive-input.png"
    disk_path = storage_root / rel_path
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_bytes(b"drive-png-bytes")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        upload = create_uploaded_file_record(
            conn,
            owner_user_id=actor["id"],
            storage_path=rel_path,
            privacy_mode="standard_plain",
            size_bytes=len(b"drive-png-bytes"),
            original_filename="drive-input.png",
            mime_type="image/png",
            user=actor,
        )
        conn.execute("UPDATE uploaded_files SET scan_status='not_required' WHERE id=?", (upload["file_id"],))
        file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (upload["file_id"],)).fetchone()
        storage_file, msg = create_storage_file_entry(
            conn,
            actor=actor,
            file_row=file_row,
            virtual_path="/inputs/drive-input.png",
            display_name="drive-input.png",
            source="test",
        )
        assert msg is None
        conn.commit()
    finally:
        conn.close()

    candidates = client.get("/api/comfyui/input-image-candidates")
    assert candidates.status_code == 200
    body = candidates.get_json()
    assert any(item["filename"] == preview["image_ref"]["filename"] for item in body["history"])
    drive_candidate = next(item for item in body["cloud_drive"] if item["file_id"] == upload["file_id"])
    assert drive_candidate["virtual_path"] == storage_file["virtual_path"]

    drive_import = client.post("/api/comfyui/import-drive-image", json={"file_id": upload["file_id"]})
    assert drive_import.status_code == 200
    drive_body = drive_import.get_json()["image"]
    assert drive_body["cloud_file_id"] == upload["file_id"]
    assert drive_body["image_ref"]["type"] == "input"
    assert drive_body["data_url"].startswith("data:image/png;base64,")

    history_import = client.post("/api/comfyui/import-history-image", json={"image_ref": preview["image_ref"]})
    assert history_import.status_code == 200
    history_body = history_import.get_json()["image"]
    assert history_body["cloud_file_id"]
    assert history_body["image_ref"]["type"] == "input"
    assert history_body["data_url"].startswith("data:image/png;base64,")
    assert len(FakeComfyUIClient.uploaded_images) >= 2


def test_comfyui_import_uploaded_image_saves_cloud_file_and_input_ref(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    FakeComfyUIClient.uploaded_images = []
    client = _build_app(db_path, storage_root).test_client()

    imported = client.post(
        "/api/comfyui/import-uploaded-image",
        data={
            "image": (io.BytesIO(b"uploaded-template-png"), "local template.png", "image/png"),
        },
        content_type="multipart/form-data",
    )
    assert imported.status_code == 200, imported.get_json()
    body = imported.get_json()["image"]
    assert body["cloud_file_id"]
    assert body["storage_file_id"]
    assert body["filename"] == "local_template.png"
    assert body["image_ref"]["filename"] == "local_template.png"
    assert body["image_ref"]["type"] == "input"
    assert body["data_url"].startswith("data:image/png;base64,")
    assert FakeComfyUIClient.uploaded_images[-1]["size"] == len(b"uploaded-template-png")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (body["cloud_file_id"],)).fetchone()
        storage_row = conn.execute("SELECT * FROM storage_files WHERE id=?", (body["storage_file_id"],)).fetchone()
    finally:
        conn.close()
    assert file_row["privacy_mode"] == "standard_plain"
    assert file_row["original_filename_plain_for_public"] == "local_template.png"
    assert storage_row["virtual_path"].startswith("/input/comfyui/")


def test_comfyui_generate_allows_unsupported_lora_base_model_with_warning_only(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    _write_lora_sidecar(comfy_base, "anime-style.safetensors", base_model="Flux")
    client = _build_app(
        db_path,
        storage_root,
        settings={"comfyui_base_dir": str(comfy_base)},
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "model": "dream.safetensors",
            "prompt": "allow flux lora",
            "loras": [{"name": "anime-style.safetensors", "strength_model": 1, "strength_clip": 1}],
            "confirm_billing": True,
        },
    )

    assert generated.status_code == 200, generated.get_json()
    _await_comfyui_result(client, generated)
    assert FakeComfyUIClient.last_params["loras"] == [{
        "name": "anime-style.safetensors",
        "strength_model": 1.0,
        "strength_clip": 1.0,
    }]


def test_comfyui_generate_async_job_reports_progress_and_result(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    started = client.post(
        "/api/comfyui/generate",
        json={
            "model": "dream.safetensors",
            "prompt": "a quiet test image",
            "width": 512,
            "height": 512,
            "steps": 12,
            "cfg": 6.5,
            "sampler_name": "euler",
            "scheduler": "normal",
            "seed": 123,
            "batch_size": 1,
            "confirm_billing": True,
            "async_progress": True,
        },
    )

    assert started.status_code == 200
    start_body = started.get_json()
    assert start_body["ok"] is True
    assert start_body["async"] is True
    job_id = start_body["job"]["job_id"]

    final_body = None
    for _ in range(100):
        polled = client.get(f"/api/comfyui/jobs/{job_id}")
        assert polled.status_code == 200
        final_body = polled.get_json()
        assert final_body["ok"] is True
        assert "percent" in (final_body["job"]["progress"] or {})
        if final_body["job"]["status"] == "completed":
            break
        time.sleep(0.1)

    assert final_body is not None
    assert final_body["job"]["status"] == "completed"
    assert final_body["job"]["progress"]["percent"] == 100
    assert final_body["job"]["result"]["image"]["image_ref"]["filename"] == "hackme_web_00001_.png"


def test_comfyui_generate_async_job_captures_request_meta_before_thread_handoff(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    audit_rows = []

    def audit(event, ip, **kwargs):
        audit_rows.append({
            "event": event,
            "ip": ip,
            "ua": kwargs.get("ua"),
            "detail": kwargs.get("detail"),
        })

    client = _build_app(
        db_path,
        storage_root,
        extra_deps={
            "audit": audit,
            "get_client_ip": lambda: request.headers.get("X-Forwarded-For", "-"),
            "get_ua": lambda: request.headers.get("User-Agent", "-"),
        },
    ).test_client()

    started = client.post(
        "/api/comfyui/generate",
        json={
            "model": "dream.safetensors",
            "prompt": "a quiet test image",
            "width": 512,
            "height": 512,
            "steps": 12,
            "cfg": 6.5,
            "sampler_name": "euler",
            "scheduler": "normal",
            "seed": 123,
            "batch_size": 1,
            "confirm_billing": True,
            "async_progress": True,
        },
        headers={
            "User-Agent": "pytest-agent/1.0",
            "X-Forwarded-For": "203.0.113.9",
        },
    )

    assert started.status_code == 200
    job_id = started.get_json()["job"]["job_id"]

    final_body = None
    for _ in range(40):
        polled = client.get(f"/api/comfyui/jobs/{job_id}")
        assert polled.status_code == 200
        final_body = polled.get_json()
        if final_body["job"]["status"] == "completed":
            break
        time.sleep(0.05)

    assert final_body is not None
    assert final_body["job"]["status"] == "completed"
    assert any(
        row["event"] == "COMFYUI_GENERATE"
        and row["ip"] == "203.0.113.9"
        and row["ua"] == "pytest-agent/1.0"
        for row in audit_rows
    )


def test_comfyui_async_job_status_survives_worker_handoff(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)

    app_a = _build_app(
        db_path,
        storage_root,
        extra_deps={
            "comfyui_generation_jobs": {},
            "comfyui_generation_jobs_lock": threading.Lock(),
        },
    )
    app_b = _build_app(
        db_path,
        storage_root,
        extra_deps={
            "comfyui_generation_jobs": {},
            "comfyui_generation_jobs_lock": threading.Lock(),
        },
    )

    started = app_a.test_client().post(
        "/api/comfyui/generate",
        json={
            "model": "dream.safetensors",
            "prompt": "worker handoff generation",
            "width": 512,
            "height": 512,
            "steps": 12,
            "cfg": 6.5,
            "sampler_name": "euler",
            "scheduler": "normal",
            "seed": 123,
            "batch_size": 1,
            "confirm_billing": True,
            "async_progress": True,
        },
    )

    final_job = _await_comfyui_job(app_b.test_client(), started)
    assert final_job["status"] == "completed"
    assert final_job["result"]["image"]["image_ref"]["filename"] == "hackme_web_00001_.png"


def test_comfyui_async_generation_worker_survives_logout_or_session_change(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor_box = {"actor": _actor()}
    started = threading.Event()
    release = threading.Event()

    class SlowSessionIndependentClient(FakeComfyUIClient):
        def generate_image(self, params, *, timeout_seconds=180, progress_callback=None):
            started.set()
            if progress_callback:
                progress_callback({
                    "phase": "running",
                    "percent": 25,
                    "current": 1,
                    "max": 4,
                    "current_node": "3",
                    "detail": "session independence check",
                    "queue_remaining": 0,
                })
            assert release.wait(timeout=10)
            return super().generate_image(params, timeout_seconds=timeout_seconds, progress_callback=progress_callback)

    client = _build_app(
        db_path,
        storage_root,
        comfyui_client=SlowSessionIndependentClient(),
        actor=lambda: actor_box["actor"],
    ).test_client()

    started_response = client.post(
        "/api/comfyui/generate",
        json={
            "model": "dream.safetensors",
            "prompt": "session independent generation",
            "width": 512,
            "height": 512,
            "steps": 12,
            "cfg": 6.5,
            "sampler_name": "euler",
            "scheduler": "normal",
            "seed": 123,
            "batch_size": 1,
            "confirm_billing": True,
            "async_progress": True,
        },
    )
    assert started_response.status_code == 200
    job_id = started_response.get_json()["job"]["job_id"]
    assert started.wait(timeout=10)

    actor_box["actor"] = None
    logged_out_status = client.get(f"/api/comfyui/jobs/{job_id}")
    assert logged_out_status.status_code == 401
    actor_box["actor"] = {"id": 2, "username": "bob", "role": "user", "member_level": "trusted", "effective_level": "trusted"}
    other_user_status = client.get(f"/api/comfyui/jobs/{job_id}")
    assert other_user_status.status_code == 403

    release.set()
    platform_job = None
    for _ in range(200):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            platform_job = conn.execute(
                """
                SELECT * FROM job_center_jobs
                WHERE source_module='comfyui' AND source_ref=? AND status='succeeded'
                """,
                (job_id,),
            ).fetchone()
        finally:
            conn.close()
        if platform_job:
            break
        time.sleep(0.05)

    assert platform_job is not None
    assert platform_job["owner_user_id"] == 1
    actor_box["actor"] = _actor()
    final_status = client.get(f"/api/comfyui/jobs/{job_id}")
    assert final_status.status_code == 200
    final_body = final_status.get_json()
    assert final_body["job"]["status"] == "completed"
    assert final_body["job"]["result"]["image"]["image_ref"]["filename"] == "hackme_web_00001_.png"


def test_comfyui_batch_limit_is_root_configurable(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    points = FakePointsService(balance=100)
    client = _build_app(db_path, storage_root, settings={"comfyui_max_batch_size": 3}, points_service=points).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "model": "dream.safetensors",
            "prompt": "a quiet test image",
            "seed": 123,
            "batch_size": 3,
            "confirm_billing": True,
        },
    )
    assert generated.status_code == 200
    body = _await_comfyui_result(client, generated)
    assert body["image"]["batch_size"] == 3
    assert len(body["images"]) == 3
    assert body["images"][2]["image_ref"]["filename"] == "hackme_web_00003_.png"
    assert body["billing"]["charged"] is True
    assert body["billing"]["total_price"] == 15
    assert points.spends == [{
        "user_id": 1,
        "item_key": "comfyui_txt2img_basic",
        "quantity": 3,
        "reference_type": "comfyui_generation",
        "reference_id": "prompt-1",
        "metadata": {
            "charged_after_success": True,
            "unit_price": 5,
            "quantity": 3,
            "lora_count": 0,
            "lora_extra_unit_price": 1,
            "lora_extra_price": 0,
            "total_price": 15,
        },
        "amount": 15,
    }]


def test_comfyui_default_dimensions_are_root_configurable(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={"comfyui_default_width": 768, "comfyui_default_height": 1024},
    ).test_client()

    models = client.get("/api/comfyui/models")
    status = client.get("/api/comfyui/status")
    generated = client.post(
        "/api/comfyui/generate",
        json={"model": "dream.safetensors", "prompt": "use configured size", "seed": 123, "confirm_billing": True},
    )

    assert models.get_json()["default_width"] == 768
    assert models.get_json()["default_height"] == 1024
    assert status.get_json()["default_width"] == 768
    assert status.get_json()["default_height"] == 1024
    assert generated.status_code == 200
    _await_comfyui_result(client, generated)
    assert FakeComfyUIClient.last_params["width"] == 768
    assert FakeComfyUIClient.last_params["height"] == 1024


def test_comfyui_generation_failure_does_not_charge_points(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    points = FakePointsService(balance=100)
    client = _build_app(db_path, storage_root, comfyui_client=FailingComfyUIClient(), points_service=points).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={"model": "dream.safetensors", "prompt": "this will fail", "seed": 123, "confirm_billing": True},
    )

    job = _await_comfyui_job(client, generated, expected_status="error")
    assert "ComfyUI 產圖失敗" in job["error"]
    assert points.spends == []
    assert points.balance == 100


def test_comfyui_generation_rejects_when_points_are_insufficient_before_work(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    points = FakePointsService(balance=4)
    client = _build_app(db_path, storage_root, points_service=points).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={"model": "dream.safetensors", "prompt": "too expensive", "seed": 123},
    )

    assert generated.status_code == 409
    assert "積分不足" in generated.get_json()["msg"]
    assert points.spends == []


def test_comfyui_billing_quote_rejects_total_run_cost_before_work(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    points = FakePointsService(balance=45)
    FakeComfyUIClient.generated_count = 0
    client = _build_app(db_path, storage_root, points_service=points).test_client()

    quoted = client.post(
        "/api/comfyui/billing-quote",
        json={
            "model": "dream.safetensors",
            "prompt": "ten images",
            "seed": 123,
            "batch_size": 1,
            "run_count": 10,
        },
    )

    body = quoted.get_json()
    assert quoted.status_code == 409
    assert "積分不足" in body["msg"]
    assert body["billing"]["quantity"] == 10
    assert body["billing"]["total_price"] == 50
    assert body["billing"]["run_count"] == 10
    assert points.spends == []
    assert FakeComfyUIClient.generated_count == 0


def test_comfyui_lora_billing_quote_adds_one_point_per_lora_per_image(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    _write_lora_sidecar(comfy_base, "detail.safetensors", base_model="SDXL")
    _write_lora_sidecar(comfy_base, "anime-style.safetensors", base_model="Pony")
    points = FakePointsService(balance=100)
    client = _build_app(
        db_path,
        storage_root,
        settings={"comfyui_base_dir": str(comfy_base)},
        points_service=points,
    ).test_client()

    quoted = client.post(
        "/api/comfyui/billing-quote",
        json={
            "model": "dream.safetensors",
            "prompt": "lora quote",
            "seed": 123,
            "batch_size": 1,
            "run_count": 2,
            "loras": [{"name": "detail.safetensors"}, {"name": "anime-style.safetensors"}],
        },
    )

    assert quoted.status_code == 200
    billing = quoted.get_json()["billing"]
    assert billing["quantity"] == 2
    assert billing["lora_count"] == 2
    assert billing["base_price_total"] == 10
    assert billing["lora_extra_price"] == 4
    assert billing["total_price"] == 14


def test_comfyui_generation_requires_billing_confirmation_for_non_root(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    points = FakePointsService(balance=100)
    FakeComfyUIClient.generated_count = 0
    client = _build_app(db_path, storage_root, points_service=points).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={"model": "dream.safetensors", "prompt": "needs confirmation", "seed": 123},
    )

    body = generated.get_json()
    assert generated.status_code == 409
    assert body["ok"] is False
    assert "請先確認扣點" in body["msg"]
    assert body["billing"]["confirmation_required"] is True
    assert points.spends == []
    assert points.balance == 100
    assert FakeComfyUIClient.generated_count == 0


def test_comfyui_generation_does_not_charge_root(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    points = FakePointsService(balance=0)
    root_actor = {
        "id": 1,
        "username": "root",
        "role": "super_admin",
        "member_level": "trusted",
        "effective_level": "trusted",
        "sanction_status": "none",
    }
    client = _build_app(db_path, storage_root, actor=lambda: root_actor, points_service=points).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={"model": "dream.safetensors", "prompt": "root free", "seed": 123},
    )

    assert generated.status_code == 200
    body = _await_comfyui_result(client, generated)
    assert body["billing"] == {"charged": False, "exempt": "root"}
    assert points.spends == []

def test_comfyui_workflow_uses_requested_batch_size():
    workflow = ComfyUIClient("http://fake-comfyui").build_text_to_image_workflow({
        "model": "dream.safetensors",
        "prompt": "batch test",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 12,
        "cfg": 6.5,
        "sampler_name": "euler",
        "scheduler": "normal",
        "seed": 123,
        "batch_size": 4,
        "filename_prefix": "hackme_web",
    })

    assert workflow["5"]["inputs"]["batch_size"] == 4


def test_comfyui_builds_sdxl_gguf_text_to_image_workflow():
    workflow = ComfyUIClient("http://fake-comfyui").build_generation_workflow({
        "generation_mode": "txt2img",
        "model": WAI_GGUF_FILE,
        "comfyui_gguf_unet_name": WAI_GGUF_FILE,
        "clip_loader_class": "DualCLIPLoaderGGUF",
        "clip_type": "sdxl",
        "clip": WAI_CLIP_L,
        "clip2": WAI_CLIP_G,
        "vae": WAI_VAE,
        "prompt": "gguf prompt",
        "negative_prompt": "bad",
        "width": 1024,
        "height": 1024,
        "steps": 24,
        "cfg": 7,
        "sampler_name": "euler",
        "scheduler": "normal",
        "seed": 123,
        "batch_size": 1,
        "filename_prefix": "hackme_web_gguf",
    })

    assert workflow["4"]["class_type"] == "UnetLoaderGGUF"
    assert workflow["4"]["inputs"]["unet_name"] == WAI_GGUF_FILE
    assert workflow["10"]["class_type"] == "DualCLIPLoaderGGUF"
    assert workflow["10"]["inputs"]["clip_name1"] == WAI_CLIP_L
    assert workflow["10"]["inputs"]["clip_name2"] == WAI_CLIP_G
    assert workflow["10"]["inputs"]["type"] == "sdxl"
    assert "device" not in workflow["10"]["inputs"]
    assert workflow["3"]["inputs"]["model"] == ["4", 0]
    assert workflow["8"]["inputs"]["vae"] == ["11", 0]


def test_standalone_gguf_probe_profile_config_overrides_common_defaults():
    script_path = ROOT / "scripts" / "comfyui" / "standalone_gguf_txt2img.py"
    spec = importlib.util.spec_from_file_location("standalone_gguf_txt2img", script_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config = {
        "common": {"steps": 24, "cfg": 5.0, "sampler": "euler", "scheduler": "normal"},
        "gguf": {"gguf_profile": "btaskel"},
        "gguf_profiles": {
            "btaskel": {
                "steps": 24,
                "cfg": 6.0,
                "sampler": "dpmpp_2m",
                "scheduler": "karras",
                "model": "btaskel/Illustrious-XL-v2.0-GGUF",
            }
        },
    }

    merged = module._config_sections(config, ("gguf",), profile_id="btaskel")
    assert merged["model"] == "btaskel/Illustrious-XL-v2.0-GGUF"
    assert merged["cfg"] == 6.0
    assert merged["sampler"] == "dpmpp_2m"
    assert merged["scheduler"] == "karras"


def test_comfyui_builds_sd35_gguf_text_to_image_workflow_with_sd3_nodes():
    workflow = ComfyUIClient("http://fake-comfyui").build_generation_workflow({
        "generation_mode": "txt2img",
        "model": SD35_GGUF_FILE,
        "comfyui_gguf_unet_name": SD35_GGUF_FILE,
        "workflow_family": "sd3_triple_clip_gguf",
        "clip_loader_class": "TripleCLIPLoader",
        "clip": SD35_CLIP_G,
        "clip2": SD35_CLIP_L,
        "clip3": SD35_T5,
        "vae": SD35_VAE,
        "prompt": "sd3 prompt",
        "negative_prompt": "bad",
        "width": 1024,
        "height": 1024,
        "sd3_native_width": 1344,
        "sd3_native_height": 768,
        "output_width": 1920,
        "output_height": 1080,
        "output_upscale_method": "lanczos",
        "steps": 40,
        "cfg": 4.5,
        "sampler_name": "dpmpp_2m",
        "scheduler": "sgm_uniform",
        "seed": 123,
        "batch_size": 1,
        "filename_prefix": "hackme_web_sd35",
        "sd3_shift": 3.0,
        "sd3_negative_split": 0.1,
    })

    class_types = {node["class_type"] for node in workflow.values()}
    assert {
        "UnetLoaderGGUF",
        "TripleCLIPLoader",
        "ModelSamplingSD3",
        "EmptySD3LatentImage",
        "ConditioningZeroOut",
        "ConditioningSetTimestepRange",
        "ConditioningCombine",
    } <= class_types
    model_sampling_node = next(key for key, node in workflow.items() if node["class_type"] == "ModelSamplingSD3")
    latent_node = next(key for key, node in workflow.items() if node["class_type"] == "EmptySD3LatentImage")
    negative_node = next(key for key, node in workflow.items() if node["class_type"] == "ConditioningCombine")
    scale_node = next(key for key, node in workflow.items() if node["class_type"] == "ImageScale")
    assert workflow["10"]["inputs"]["clip_name1"] == SD35_CLIP_G
    assert workflow["10"]["inputs"]["clip_name2"] == SD35_CLIP_L
    assert workflow["10"]["inputs"]["clip_name3"] == SD35_T5
    assert workflow[model_sampling_node]["inputs"]["shift"] == 3.0
    assert workflow["3"]["inputs"]["model"] == [model_sampling_node, 0]
    assert workflow["3"]["inputs"]["latent_image"] == [latent_node, 0]
    assert workflow["3"]["inputs"]["negative"] == [negative_node, 0]
    assert workflow["3"]["inputs"]["scheduler"] == "sgm_uniform"
    assert workflow[latent_node]["inputs"]["width"] == 1344
    assert workflow[latent_node]["inputs"]["height"] == 768
    assert workflow[scale_node]["inputs"]["width"] == 1920
    assert workflow[scale_node]["inputs"]["height"] == 1080
    assert workflow["9"]["inputs"]["images"] == [scale_node, 0]


def test_comfyui_object_info_combo_options_are_parsed_for_upscale_models(monkeypatch):
    client = ComfyUIClient("http://fake-comfyui")

    def fake_json_request(path, **_kwargs):
        assert path == "/object_info/UpscaleModelLoader"
        return {
            "UpscaleModelLoader": {
                "input": {
                    "required": {
                        "model_name": [
                            "COMBO",
                            {
                                "options": ["4x_NMKD-Superscale-SP_178000_G.pth"],
                            },
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr(client, "_json_request", fake_json_request)
    assert client.get_upscale_models() == ["4x_NMKD-Superscale-SP_178000_G.pth"]


def test_comfyui_object_info_combo_options_are_parsed_for_latent_upscale_models(monkeypatch):
    client = ComfyUIClient("http://fake-comfyui")

    def fake_json_request(path, **_kwargs):
        assert path == "/object_info/LatentUpscaleModelLoader"
        return {
            "LatentUpscaleModelLoader": {
                "input": {
                    "required": {
                        "model_name": [
                            "COMBO",
                            {
                                "options": ["3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"],
                            },
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr(client, "_json_request", fake_json_request)
    assert client.get_latent_upscale_models() == ["3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"]


def test_comfyui_controlnet_models_fall_back_to_model_folder_api(monkeypatch):
    client = ComfyUIClient("http://fake-comfyui")
    seen_paths = []

    def fake_json_request(path, **_kwargs):
        seen_paths.append(path)
        if path == "/object_info/ControlNetLoader":
            return {
                "ControlNetLoader": {
                    "input": {
                        "required": {
                            "control_net_name": ["COMBO", {"options": []}],
                        }
                    }
                }
            }
        if path == "/api/models/controlnet":
            return ["QWEN/Qwen-Image-2512-Fun-Controlnet-Union-2602.safetensors"]
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(client, "_json_request", fake_json_request)

    assert client.get_controlnet_models() == ["QWEN/Qwen-Image-2512-Fun-Controlnet-Union-2602.safetensors"]
    assert seen_paths == ["/object_info/ControlNetLoader", "/api/models/controlnet"]


def test_comfyui_latent_upscale_models_fall_back_to_model_folder_api(monkeypatch):
    client = ComfyUIClient("http://fake-comfyui")
    seen_paths = []

    def fake_json_request(path, **_kwargs):
        seen_paths.append(path)
        if path == "/object_info/LatentUpscaleModelLoader":
            return {
                "LatentUpscaleModelLoader": {
                    "input": {
                        "required": {
                            "model_name": ["COMBO", {"options": []}],
                        }
                    }
                }
            }
        if path == "/api/models/latent_upscale_models":
            return ["3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"]
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(client, "_json_request", fake_json_request)
    assert client.get_latent_upscale_models() == ["3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"]
    assert seen_paths == ["/object_info/LatentUpscaleModelLoader", "/api/models/latent_upscale_models"]


def test_comfyui_clip_vision_models_fall_back_to_model_folder_api(monkeypatch):
    client = ComfyUIClient("http://fake-comfyui")
    seen_paths = []

    def fake_json_request(path, **_kwargs):
        seen_paths.append(path)
        if path == "/object_info/CLIPVisionLoader":
            return {
                "CLIPVisionLoader": {
                    "input": {"required": {"clip_name": ["COMBO", {"options": []}]}}
                }
            }
        if path == "/api/models/clip_vision":
            return ["sigclip_vision_patch14_384.safetensors"]
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(client, "_json_request", fake_json_request)

    assert client.get_clip_vision_models() == ["sigclip_vision_patch14_384.safetensors"]
    assert seen_paths == ["/object_info/CLIPVisionLoader", "/api/models/clip_vision"]


def test_comfyui_capabilities_include_comfyui_gguf_unet_loader_options(monkeypatch):
    client = ComfyUIClient("http://fake-comfyui")

    def fake_json_request(path, **_kwargs):
        if str(path).startswith("/api/models/"):
            return []
        assert path == "/object_info"
        return {
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
            "VAELoader": {
                "input": {"required": {"vae_name": [["sdxl_vae.safetensors"]]}}
            },
            "KSampler": {
                "input": {
                    "required": {
                        "sampler_name": [["euler"]],
                        "scheduler": [["normal"]],
                    }
                }
            },
        }

    monkeypatch.setattr(client, "_json_request", fake_json_request)
    capabilities = client.get_capabilities()

    assert capabilities["diffusion_models"] == ["WAI-NSFW-Illustrious-v140-Q8_0.gguf"]
    assert capabilities["clip_models"] == ["clip_g.safetensors", "clip_l.safetensors"]
    assert capabilities["vaes"] == ["sdxl_vae.safetensors"]


def test_comfyui_capabilities_fall_back_to_latent_upscale_model_folder_api(monkeypatch):
    client = ComfyUIClient("http://fake-comfyui")

    def fake_json_request(path, **_kwargs):
        if path == "/object_info":
            return {
                "LatentUpscaleModelLoader": {
                    "input": {
                        "required": {
                            "model_name": ["COMBO", {"options": []}],
                        }
                    }
                }
            }
        if path == "/api/models/latent_upscale_models":
            return ["3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"]
        if path == "/api/models/upscale_models":
            return []
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(client, "_json_request", fake_json_request)
    capabilities = client.get_capabilities()

    assert capabilities["latent_upscale_models"] == ["3/ltx-2.3-spatial-upscaler-x2-1.1.safetensors"]


def test_comfyui_capabilities_fall_back_to_controlnet_model_folder_api(monkeypatch):
    client = ComfyUIClient("http://fake-comfyui")

    def fake_json_request(path, **_kwargs):
        if path == "/object_info":
            return {
                "ControlNetLoader": {
                    "input": {
                        "required": {
                            "control_net_name": ["COMBO", {"options": []}],
                        }
                    }
                },
                "ControlNetApplyAdvanced": {"input": {"required": {}}},
                "LoadImage": {"input": {"required": {}}},
                "CannyEdgePreprocessor": {"input": {"required": {}}},
            }
        if path == "/api/models/controlnet":
            return ["QWEN/Qwen-Image-2512-Fun-Controlnet-Union-2602.safetensors"]
        if path in {"/api/models/upscale_models", "/api/models/latent_upscale_models", "/api/models/clip_vision"}:
            return []
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(client, "_json_request", fake_json_request)
    capabilities = client.get_capabilities()

    assert capabilities["controlnet_models"] == ["QWEN/Qwen-Image-2512-Fun-Controlnet-Union-2602.safetensors"]


def test_comfyui_capabilities_fall_back_to_clip_vision_model_folder_api(monkeypatch):
    client = ComfyUIClient("http://fake-comfyui")

    def fake_json_request(path, **_kwargs):
        if path == "/object_info":
            return {
                "CLIPVisionLoader": {
                    "input": {"required": {"clip_name": ["COMBO", {"options": []}]}}
                }
            }
        if path == "/api/models/clip_vision":
            return ["sigclip_vision_patch14_384.safetensors"]
        if path in {"/api/models/upscale_models", "/api/models/latent_upscale_models"}:
            return []
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(client, "_json_request", fake_json_request)
    capabilities = client.get_capabilities()

    assert capabilities["clip_vision_models"] == ["sigclip_vision_patch14_384.safetensors"]


def test_comfyui_workflow_dependency_status_accepts_clip_vision_models(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()
    workflow = {
        "10": {
            "class_type": "CLIPVisionLoader",
            "inputs": {"clip_name": "sigclip_vision_patch14_384.safetensors"},
        },
        "20": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["30", 0]},
        },
    }
    preset = _import_workflow_preset(client, workflow, title="Clip Vision Workflow")

    detail = client.get(f"/api/comfyui/workflows/{preset['id']}")
    assert detail.status_code == 200
    dependency = detail.get_json()["preset"]["dependency_status"]

    assert dependency["available"] is True
    assert dependency["missing_models"] == []


def test_comfyui_workflow_dependency_status_accepts_controlnet_subfolder_paths(tmp_path):
    class SubfolderControlNetClient(FakeComfyUIClient):
        def get_capabilities(self):
            capabilities = json.loads(json.dumps(super().get_capabilities()))
            capabilities["controlnet_models"] = ["SD35/sd3.5_large_controlnet_depth.safetensors"]
            capabilities["controlnet_types"]["depth"]["available"] = True
            capabilities["controlnet_types"]["depth"]["matching_models"] = [
                "SD35/sd3.5_large_controlnet_depth.safetensors"
            ]
            return capabilities

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root, comfyui_client=SubfolderControlNetClient()).test_client()
    workflow = {
        "10": {
            "class_type": "ControlNetLoader",
            "inputs": {"control_net_name": "SD35\\sd3.5_large_controlnet_depth.safetensors"},
        },
        "20": {
            "class_type": "PreviewImage",
            "inputs": {"images": ["30", 0]},
        },
    }
    preset = _import_workflow_preset(client, workflow, title="SD3.5 Depth ControlNet Workflow")

    detail = client.get(f"/api/comfyui/workflows/{preset['id']}")
    assert detail.status_code == 200
    dependency = detail.get_json()["preset"]["dependency_status"]

    assert dependency["available"] is True
    assert dependency["missing_controlnets"] == []


def test_comfyui_workflow_chains_loras_between_checkpoint_and_sampler():
    workflow = ComfyUIClient("http://fake-comfyui").build_text_to_image_workflow({
        "model": "dream.safetensors",
        "prompt": "lora test",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 12,
        "cfg": 6.5,
        "sampler_name": "euler",
        "scheduler": "normal",
        "seed": 123,
        "batch_size": 1,
        "filename_prefix": "hackme_web",
        "loras": [
            {"name": "detail.safetensors", "strength_model": 0.8, "strength_clip": 0.7},
            {"name": "anime-style.safetensors", "strength_model": 1.0, "strength_clip": 1.0},
        ],
    })

    assert workflow["10"]["class_type"] == "LoraLoader"
    assert workflow["10"]["inputs"]["model"] == ["4", 0]
    assert workflow["10"]["inputs"]["clip"] == ["4", 1]
    assert workflow["10"]["inputs"]["lora_name"] == "detail.safetensors"
    assert workflow["11"]["inputs"]["model"] == ["10", 0]
    assert workflow["11"]["inputs"]["clip"] == ["10", 1]
    assert workflow["3"]["inputs"]["model"] == ["11", 0]
    assert workflow["6"]["inputs"]["clip"] == ["11", 1]
    assert workflow["7"]["inputs"]["clip"] == ["11", 1]


def test_comfyui_inpaint_workflow_sets_grow_mask_by():
    workflow = ComfyUIClient("http://fake-comfyui").build_inpaint_workflow({
        "model": "dream.safetensors",
        "prompt": "repair the face",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 12,
        "cfg": 6.5,
        "sampler_name": "euler",
        "scheduler": "normal",
        "seed": 123,
        "batch_size": 1,
        "filename_prefix": "hackme_web",
        "source_image_ref": {"filename": "source.png", "subfolder": "", "type": "input"},
        "mask_image_ref": {"filename": "mask.png", "subfolder": "", "type": "input"},
    })

    assert workflow["10"]["class_type"] == "VAEEncodeForInpaint"
    assert workflow["10"]["inputs"]["grow_mask_by"] == 6
    assert workflow["11"]["inputs"]["channel"] == "red"


def test_comfyui_outpaint_workflow_sets_grow_mask_by():
    workflow = ComfyUIClient("http://fake-comfyui").build_outpaint_workflow({
        "model": "dream.safetensors",
        "prompt": "expand the canvas",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 12,
        "cfg": 6.5,
        "sampler_name": "euler",
        "scheduler": "normal",
        "seed": 123,
        "batch_size": 1,
        "filename_prefix": "hackme_web",
        "source_image_ref": {"filename": "source.png", "subfolder": "", "type": "input"},
        "outpaint": {"left": 64, "top": 32, "right": 16, "bottom": 8, "feathering": 24},
    })

    assert workflow["11"]["class_type"] == "VAEEncodeForInpaint"
    assert workflow["11"]["inputs"]["grow_mask_by"] == 6


def test_comfyui_workflow_uses_custom_vae_when_selected():
    workflow = ComfyUIClient("http://fake-comfyui").build_text_to_image_workflow({
        "model": "dream.safetensors",
        "prompt": "vae test",
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 12,
        "cfg": 6.5,
        "sampler_name": "euler",
        "scheduler": "normal",
        "seed": 123,
        "batch_size": 1,
        "filename_prefix": "hackme_web",
        "vae": "sdxl_vae.safetensors",
        "loras": [{"name": "detail.safetensors", "strength_model": 1.0, "strength_clip": 1.0}],
    })

    assert workflow["11"]["class_type"] == "VAELoader"
    assert workflow["11"]["inputs"]["vae_name"] == "sdxl_vae.safetensors"
    assert workflow["8"]["inputs"]["vae"] == ["11", 0]


def test_comfyui_generate_normalizes_embedding_shortcut_syntax(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "model": "dream.safetensors",
            "prompt": "portrait, <embeddings:badhandv4.pt>",
            "negative_prompt": "<embeddings:easynegative.safetensors>",
            "seed": 123,
            "confirm_billing": True,
        },
    )

    assert generated.status_code == 200
    _await_comfyui_result(client, generated)
    assert FakeComfyUIClient.last_params["prompt"] == "portrait, embedding:badhandv4.pt"
    assert FakeComfyUIClient.last_params["negative_prompt"] == "embedding:easynegative.safetensors"


def test_comfyui_status_reports_offline_backend(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    app = Flask(__name__)
    app.testing = True

    def get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    register_comfyui_routes(app, {
        "STORAGE_DIR": str(storage_root),
        "audit": lambda *args, **kwargs: None,
        "get_client_ip": lambda: "127.0.0.1",
        "get_current_user_ctx": _actor,
        "get_db": get_db,
        "get_system_settings": lambda: {"feature_comfyui_enabled": True},
        "get_member_level_rule": lambda conn, level: {},
        "get_ua": lambda: "test-agent",
        "json_resp": _json_resp,
        "require_csrf": _passthrough,
        "require_csrf_safe": _passthrough,
        "comfyui_client": OfflineComfyUIClient(),
    })

    status = app.test_client().get("/api/comfyui/status")
    assert status.status_code == 200
    body = status.get_json()
    assert body["ok"] is True
    assert body["available"] is False
    assert body["comfyui_url"] == "http://fake-offline"


def test_comfyui_status_uses_lan_default_for_legacy_blank_remote_url(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "remote",
            "comfyui_remote_api_url": "",
            "comfyui_api_host": "localhost",
            "comfyui_api_port": 8192,
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: UrlEchoComfyUIClient(url),
        },
    ).test_client()

    status = client.get("/api/comfyui/status")

    assert status.status_code == 200
    body = status.get_json()
    assert body["available"] is True
    assert body["connection_mode"] == "remote"
    assert body["comfyui_url"] == "http://127.0.0.1:8188"


def test_comfyui_status_warns_when_models_are_on_windows_mount(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    windows_mount_path = "/mnt" + "/d/share/ComfyUI_windows_portable"
    client = _build_app(
        db_path,
        storage_root,
        settings={"comfyui_base_dir": windows_mount_path},
    ).test_client()

    status = client.get("/api/comfyui/status")

    assert status.status_code == 200
    warnings = status.get_json()["storage_warnings"]
    assert warnings
    assert warnings[0]["code"] == "windows_mount_model_storage"
    assert windows_mount_path in warnings[0]["path"]


def test_root_can_test_unsaved_comfyui_endpoint(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(db_path, storage_root, actor=lambda: root_actor).test_client()

    tested = client.post("/api/root/comfyui/test-connection", json={"host": "192.168.1.20", "port": 8192})

    assert tested.status_code == 200
    body = tested.get_json()
    assert body["ok"] is True
    assert body["available"] is True
    assert body["endpoint"] == {"mode": "remote", "host": "192.168.1.20", "port": 8192}


def test_comfyui_diffusers_mode_lists_repo_and_generates_without_comfyui_nodes(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": "dhead/waiIllustriousSDXL_v150",
            "comfyui_huggingface_api_token": "hf_read_token",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: FakeDiffusersBackendClient(),
        },
    ).test_client()

    listed = client.get("/api/comfyui/models")
    assert listed.status_code == 200
    body = listed.get_json()
    assert body["ok"] is True
    assert body["connection_mode"] == "diffusers"
    assert body["models"] == ["dhead/waiIllustriousSDXL_v150"]
    assert body["loras"] == []
    assert body["samplers"] == ["diffusers-auto"]
    assert any(profile["id"] == WAI_GGUF_PROFILE_ID for profile in body["gguf_profiles"])
    assert body["installed_gguf_models"] == []

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": "dhead/waiIllustriousSDXL_v150",
            "prompt": "illustration",
            "negative_prompt": "",
            "sampler_name": "diffusers-auto",
            "scheduler": "default",
            "seed": 123,
            "confirm_billing": True,
        },
    )

    assert generated.status_code == 200, generated.get_json()
    started = generated.get_json()
    assert started["job"]["status"] == "running"
    assert started["job"]["progress"]["phase"] == "downloading"
    assert started["job"]["progress"]["backend_kind"] == "diffusers"
    assert "下載 Diffusers model：dhead/waiIllustriousSDXL_v150" in started["job"]["progress"]["detail"]
    payload = _await_comfyui_result(client, generated)
    assert payload["image"]["image_ref"]["filename"] == "hackme_web_diffusers.png"
    assert FakeDiffusersBackendClient.last_params["model"] == "dhead/waiIllustriousSDXL_v150"


def test_comfyui_installed_gguf_inventory_marks_official_profiles(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        comfyui_client=FakeNativeGgufComfyUIClient(),
    ).test_client()

    listed = client.get("/api/comfyui/models")
    assert listed.status_code == 200
    body = listed.get_json()
    assert body["installed_gguf_models"][0]["profile_id"] == WAI_GGUF_PROFILE_ID
    assert body["installed_gguf_models"][0]["variant_id"] == WAI_GGUF_VARIANT_ID
    assert body["installed_gguf_models"][0]["official_profile"] is True

    inventory = client.get("/api/comfyui/installed-gguf")
    assert inventory.status_code == 200
    payload = inventory.get_json()
    assert payload["installed_gguf_models"][0]["gguf_file"] == WAI_GGUF_FILE
    assert not any(profile["id"] == SD35_GGUF_PROFILE_ID for profile in payload["gguf_profiles"])


def test_comfyui_gguf_profiles_hide_failed_sd35_and_keep_sothmik_q8(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        comfyui_client=FakeSd35NativeGgufComfyUIClient(),
    ).test_client()

    body = client.get("/api/comfyui/models").get_json()
    profiles = {profile["id"]: profile for profile in body["gguf_profiles"]}
    assert SOTHMIK_WAI_GGUF_PROFILE_ID in profiles
    assert CALCUIS_ILLUSTRIOUS_GGUF_PROFILE_ID in profiles
    assert DIVING_ILLUSTRIOUS_GGUF_PROFILE_ID in profiles
    assert BTASKEL_ILLUSTRIOUS_GGUF_PROFILE_ID not in profiles
    assert SD35_GGUF_PROFILE_ID not in profiles
    assert profiles[CALCUIS_ILLUSTRIOUS_GGUF_PROFILE_ID]["variants"][0]["gguf_file"] == CALCUIS_ILLUSTRIOUS_GGUF_FILE
    assert profiles[DIVING_ILLUSTRIOUS_GGUF_PROFILE_ID]["variants"][0]["gguf_file"] == DIVING_ILLUSTRIOUS_GGUF_FILE
    assert body["installed_gguf_models"][0]["profile_id"] == SD35_GGUF_PROFILE_ID
    assert body["installed_gguf_models"][0]["variant_id"] == SD35_GGUF_VARIANT_ID
    assert body["installed_gguf_models"][0]["enabled"] is False
    assert body["installed_gguf_models"][0]["status"] == "failed_visual_reprobe"


def test_comfyui_diffusers_mode_auto_routes_native_gguf_to_comfyui_workflow(tmp_path):
    class AutoRouteDiffusersBackendClient(FakeDiffusersBackendClient):
        def prepare_gguf_file_for_backend(self, model_repo, gguf_file, *, progress_callback=None, log_capture=None):
            if progress_callback:
                progress_callback({
                    "phase": "routing",
                    "percent": 23,
                    "backend_kind": "comfyui_gguf",
                    "step": "GGUF backend 已判斷",
                    "current_file": gguf_file,
                    "detail": "偵測為 ComfyUI-GGUF 原生 UNet",
                })
            return {
                "path": str(tmp_path / WAI_GGUF_FILE),
                "stats": {"cache_hit": True, "bytes_written": 1, "total_bytes": 1},
                "metadata": {"has_comfy_metadata": True, "has_original_unet_names": True},
                "suggested_backend": "comfyui_gguf",
                "model_repo": model_repo,
                "gguf_file": gguf_file,
            }

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)

    def factory(url):
        if str(url).startswith("diffusers://"):
            return AutoRouteDiffusersBackendClient()
        return FakeNativeGgufComfyUIClient()

    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": WAI_GGUF_REPO,
            "comfyui_remote_api_url": "http://127.0.0.1:8188",
            "comfyui_huggingface_api_token": "hf_read_token",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": factory,
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": WAI_GGUF_REPO,
            "diffusers_model_repo": WAI_GGUF_REPO,
            "diffusers_gguf_profile": WAI_GGUF_PROFILE_ID,
            "diffusers_gguf_variant": WAI_GGUF_VARIANT_ID,
            "diffusers_gguf_file": WAI_GGUF_FILE,
            "diffusers_model_variant": f"gguf::{WAI_GGUF_FILE}",
            "prompt": "illustration",
            "negative_prompt": "",
            "sampler_name": "diffusers-auto",
            "scheduler": "default",
            "seed": 123,
            "confirm_billing": True,
        },
    )

    payload = _await_comfyui_result(client, generated)
    assert payload["image"]["image_ref"]["filename"] == "hackme_web_00001_.png"
    assert FakeComfyUIClient.last_params["backend_kind"] == "comfyui_gguf"
    assert FakeComfyUIClient.last_params["model"] == WAI_GGUF_FILE
    assert FakeComfyUIClient.last_params["clip_loader_class"] == "DualCLIPLoaderGGUF"
    assert FakeComfyUIClient.last_params["clip"] == WAI_CLIP_L
    assert FakeComfyUIClient.last_params["clip2"] == WAI_CLIP_G
    assert FakeComfyUIClient.last_params["vae"] == WAI_VAE
    assert FakeComfyUIClient.last_params["diffusers_gguf_profile"] == WAI_GGUF_PROFILE_ID
    assert FakeComfyUIClient.last_params["diffusers_gguf_variant"] == WAI_GGUF_VARIANT_ID
    assert FakeComfyUIClient.last_params["sampler_name"] == "euler"
    assert FakeComfyUIClient.last_params["scheduler"] == "normal"


def test_comfyui_diffusers_mode_auto_downloads_missing_gguf_companions(tmp_path):
    prepared_dir = tmp_path / "hf-cache"
    prepared_dir.mkdir()
    comfyui_base = tmp_path / "ComfyUI"
    (comfyui_base / "models").mkdir(parents=True)
    downloaded = []

    class AutoRouteDiffusersBackendClient(FakeDiffusersBackendClient):
        def prepare_gguf_file_for_backend(self, model_repo, gguf_file, *, progress_callback=None, log_capture=None):
            path = prepared_dir / gguf_file
            path.write_bytes(b"fake-gguf")
            return {
                "path": str(path),
                "stats": {"cache_hit": True, "bytes_written": path.stat().st_size, "total_bytes": path.stat().st_size},
                "metadata": {"has_comfy_metadata": True, "has_original_unet_names": True},
                "suggested_backend": "comfyui_gguf",
                "model_repo": model_repo,
                "gguf_file": gguf_file,
            }

        def prepare_huggingface_file(
            self,
            model_repo,
            filename,
            *,
            progress_callback=None,
            log_capture=None,
            label="Hugging Face 檔案",
            base_percent=24,
            span_percent=4,
        ):
            path = prepared_dir / filename
            path.write_bytes(f"fake-{filename}".encode("utf-8"))
            downloaded.append((model_repo, filename, label))
            if progress_callback:
                progress_callback({
                    "phase": "downloading",
                    "percent": base_percent,
                    "backend_kind": "diffusers",
                    "step": f"下載 {label}",
                    "current_file": filename,
                    "detail": f"下載 {filename}",
                })
            return {
                "path": str(path),
                "stats": {"cache_hit": False, "bytes_written": path.stat().st_size, "total_bytes": path.stat().st_size},
                "model_repo": model_repo,
                "filename": filename,
            }

    class LocalMissingCompanionNativeGgufComfyUIClient(FakeNativeGgufComfyUIClient):
        base_url = "http://127.0.0.1:8188"

        def get_capabilities(self):
            payload = super().get_capabilities()
            payload["diffusion_models"] = []
            payload["clip_models"] = []
            payload["vaes"] = []
            return payload

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)

    def factory(url):
        if str(url).startswith("diffusers://"):
            return AutoRouteDiffusersBackendClient()
        return LocalMissingCompanionNativeGgufComfyUIClient()

    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_diffusers_model_repo": WAI_GGUF_REPO,
            "comfyui_api_host": "127.0.0.1",
            "comfyui_api_port": 8188,
            "comfyui_base_dir": str(comfyui_base),
            "comfyui_huggingface_api_token": "hf_read_token",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": factory,
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": WAI_GGUF_REPO,
            "diffusers_model_repo": WAI_GGUF_REPO,
            "diffusers_gguf_profile": WAI_GGUF_PROFILE_ID,
            "diffusers_gguf_variant": WAI_GGUF_VARIANT_ID,
            "diffusers_gguf_file": WAI_GGUF_FILE,
            "prompt": "illustration",
            "negative_prompt": "",
            "backend_url": "diffusers://frontend",
            "confirm_billing": True,
        },
    )

    payload = _await_comfyui_result(client, generated)
    assert payload["image"]["image_ref"]["filename"] == "hackme_web_00001_.png"
    assert (comfyui_base / "models" / "unet" / WAI_GGUF_FILE).read_bytes() == b"fake-gguf"
    assert (comfyui_base / "models" / "text_encoders" / WAI_CLIP_L).exists()
    assert (comfyui_base / "models" / "text_encoders" / WAI_CLIP_G).exists()
    assert (comfyui_base / "models" / "vae" / WAI_VAE).exists()
    assert {item[1] for item in downloaded} == {WAI_CLIP_L, WAI_CLIP_G, WAI_VAE}
    assert FakeComfyUIClient.last_params["model"] == WAI_GGUF_FILE
    assert FakeComfyUIClient.last_params["clip"] == WAI_CLIP_L
    assert FakeComfyUIClient.last_params["clip2"] == WAI_CLIP_G
    assert FakeComfyUIClient.last_params["vae"] == WAI_VAE


def test_comfyui_diffusers_mode_local_native_gguf_skips_download_when_installed(tmp_path):
    class AutoRouteDiffusersBackendClient(FakeDiffusersBackendClient):
        def prepare_gguf_file_for_backend(self, model_repo, gguf_file, *, progress_callback=None, log_capture=None):
            raise AssertionError("installed local GGUF UNet should not be downloaded again")

        def prepare_huggingface_file(self, *args, **kwargs):
            raise AssertionError("installed GGUF companions should not be downloaded again")

    class LocalInstalledGgufComfyUIClient(FakeNativeGgufComfyUIClient):
        base_url = "http://127.0.0.1:8188"

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)

    def factory(url):
        if str(url).startswith("diffusers://"):
            return AutoRouteDiffusersBackendClient()
        return LocalInstalledGgufComfyUIClient()

    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_diffusers_model_repo": WAI_GGUF_REPO,
            "comfyui_api_host": "127.0.0.1",
            "comfyui_api_port": 8188,
            "comfyui_base_dir": str(tmp_path / "ComfyUI"),
            "comfyui_huggingface_api_token": "hf_read_token",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": factory,
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": WAI_GGUF_REPO,
            "diffusers_model_repo": WAI_GGUF_REPO,
            "diffusers_gguf_profile": WAI_GGUF_PROFILE_ID,
            "diffusers_gguf_variant": WAI_GGUF_VARIANT_ID,
            "diffusers_gguf_file": WAI_GGUF_FILE,
            "prompt": "illustration",
            "negative_prompt": "",
            "backend_url": "diffusers://frontend",
            "confirm_billing": True,
        },
    )

    payload = _await_comfyui_result(client, generated)
    assert payload["image"]["image_ref"]["filename"] == "hackme_web_00001_.png"
    assert FakeComfyUIClient.last_params["backend_kind"] == "comfyui_gguf"
    assert FakeComfyUIClient.last_params["model"] == WAI_GGUF_FILE
    assert FakeComfyUIClient.last_params["clip"] == WAI_CLIP_L
    assert FakeComfyUIClient.last_params["clip2"] == WAI_CLIP_G
    assert FakeComfyUIClient.last_params["vae"] == WAI_VAE


def test_comfyui_diffusers_mode_remote_missing_gguf_reports_without_download(tmp_path):
    class UnexpectedGgufDownloadClient(FakeDiffusersBackendClient):
        def prepare_gguf_file_for_backend(self, *args, **kwargs):
            pytest.fail("remote-missing GGUF must be reported before any local Hugging Face download")

        def prepare_huggingface_file(self, *args, **kwargs):
            pytest.fail("remote-missing GGUF companions must not be downloaded locally")

    class RemoteMissingNativeGgufComfyUIClient(FakeNativeGgufComfyUIClient):
        base_url = "http://127.0.0.1:8188"

        def get_capabilities(self):
            payload = super().get_capabilities()
            payload["diffusion_models"] = []
            payload["clip_models"] = []
            payload["vaes"] = []
            return payload

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)

    def factory(url):
        if str(url).startswith("diffusers://"):
            return UnexpectedGgufDownloadClient()
        return RemoteMissingNativeGgufComfyUIClient()

    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": WAI_GGUF_REPO,
            "comfyui_remote_api_url": "http://127.0.0.1:8188",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": factory,
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": WAI_GGUF_REPO,
            "diffusers_model_repo": WAI_GGUF_REPO,
            "diffusers_gguf_profile": WAI_GGUF_PROFILE_ID,
            "diffusers_gguf_variant": WAI_GGUF_VARIANT_ID,
            "diffusers_gguf_file": WAI_GGUF_FILE,
            "prompt": "illustration",
            "negative_prompt": "",
            "confirm_billing": True,
        },
    )

    job = _await_comfyui_job(client, generated, expected_status="error")
    assert "遠端 ComfyUI 未安裝 GGUF UNet" in job["error"]
    assert WAI_GGUF_FILE in job["error"]


def test_comfyui_diffusers_mode_rejects_failed_sd35_gguf_before_download(tmp_path):
    class UnexpectedGgufDownloadClient(FakeDiffusersBackendClient):
        def prepare_gguf_file_for_backend(self, model_repo, gguf_file, *, progress_callback=None, log_capture=None):
            pytest.fail("failed SD35 GGUF must be rejected before any download/prepare step")

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)

    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": SD35_GGUF_REPO,
            "comfyui_remote_api_url": "http://127.0.0.1:8188",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: UnexpectedGgufDownloadClient(),
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": SD35_GGUF_REPO,
            "diffusers_model_repo": SD35_GGUF_REPO,
            "diffusers_gguf_profile": SD35_GGUF_PROFILE_ID,
            "diffusers_gguf_variant": SD35_GGUF_VARIANT_ID,
            "diffusers_gguf_file": SD35_GGUF_FILE,
            "diffusers_model_variant": f"gguf::{SD35_GGUF_FILE}",
            "prompt": "illustration",
            "negative_prompt": "",
            "sampler_name": "diffusers-auto",
            "scheduler": "default",
            "steps": 28,
            "cfg": 4.5,
            "width": 1920,
            "height": 1080,
            "seed": 123,
            "confirm_billing": True,
        },
    )

    assert generated.status_code == 400
    body = generated.get_json()
    assert "生成圖異常" in body["msg"]
    assert "暫不開放" in body["msg"]


def test_comfyui_diffusers_mode_rejects_unmapped_gguf_before_download(tmp_path):
    class UnexpectedGgufDownloadClient(FakeDiffusersBackendClient):
        def prepare_gguf_file_for_backend(self, *args, **kwargs):
            pytest.fail("arbitrary GGUF must be rejected before any download/prepare step")

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": "someone/random-gguf",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: UnexpectedGgufDownloadClient(),
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": "someone/random-gguf",
            "diffusers_model_repo": "someone/random-gguf",
            "diffusers_gguf_file": "random-Q8_0.gguf",
            "diffusers_model_variant": "gguf::random-Q8_0.gguf",
            "prompt": "illustration",
            "negative_prompt": "",
            "sampler_name": "diffusers-auto",
            "scheduler": "default",
            "seed": 123,
            "confirm_billing": True,
        },
    )

    assert generated.status_code == 409
    assert "GGUF 只允許官方已建檔 profile" in generated.get_json()["msg"]


def test_comfyui_diffusers_mode_rejects_disabled_gguf_profile_before_download(tmp_path):
    class UnexpectedGgufDownloadClient(FakeDiffusersBackendClient):
        def prepare_gguf_file_for_backend(self, *args, **kwargs):
            pytest.fail("disabled GGUF profiles must be rejected before any download/prepare step")

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": BTASKEL_ILLUSTRIOUS_GGUF_REPO,
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: UnexpectedGgufDownloadClient(),
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": BTASKEL_ILLUSTRIOUS_GGUF_REPO,
            "diffusers_model_repo": BTASKEL_ILLUSTRIOUS_GGUF_REPO,
            "diffusers_gguf_profile": BTASKEL_ILLUSTRIOUS_GGUF_PROFILE_ID,
            "diffusers_gguf_variant": BTASKEL_ILLUSTRIOUS_GGUF_VARIANT_ID,
            "diffusers_gguf_file": BTASKEL_ILLUSTRIOUS_GGUF_FILE,
            "diffusers_model_variant": f"gguf::{BTASKEL_ILLUSTRIOUS_GGUF_FILE}",
            "prompt": "illustration",
            "negative_prompt": "",
            "sampler_name": "diffusers-auto",
            "scheduler": "default",
            "seed": 123,
            "confirm_billing": True,
        },
    )

    assert generated.status_code == 400
    assert "尚未通過本站驗證" in generated.get_json()["msg"]


def test_comfyui_diffusers_mode_local_native_gguf_installs_cached_file(tmp_path):
    cached_gguf = tmp_path / "hf-cache" / WAI_GGUF_FILE
    cached_gguf.parent.mkdir(parents=True)
    cached_gguf.write_bytes(b"fake gguf")

    class AutoRouteDiffusersBackendClient(FakeDiffusersBackendClient):
        def prepare_gguf_file_for_backend(self, model_repo, gguf_file, *, progress_callback=None, log_capture=None):
            return {
                "path": str(cached_gguf),
                "stats": {"cache_hit": True, "bytes_written": cached_gguf.stat().st_size, "total_bytes": cached_gguf.stat().st_size},
                "metadata": {"has_comfy_metadata": True, "has_original_unet_names": True},
                "suggested_backend": "comfyui_gguf",
                "model_repo": model_repo,
                "gguf_file": gguf_file,
            }

    class LocalMissingGgufComfyUIClient(FakeNativeGgufComfyUIClient):
        base_url = "http://127.0.0.1:8188"

        def get_capabilities(self):
            payload = super().get_capabilities()
            payload["diffusion_models"] = []
            return payload

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    comfy_base = tmp_path / "ComfyUI"
    storage_root.mkdir()
    _init_db(db_path)

    def factory(url):
        if str(url).startswith("diffusers://"):
            return AutoRouteDiffusersBackendClient()
        return LocalMissingGgufComfyUIClient()

    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_diffusers_model_repo": WAI_GGUF_REPO,
            "comfyui_api_host": "127.0.0.1",
            "comfyui_api_port": 8188,
            "comfyui_base_dir": str(comfy_base),
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": factory,
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": WAI_GGUF_REPO,
            "diffusers_model_repo": WAI_GGUF_REPO,
            "diffusers_gguf_profile": WAI_GGUF_PROFILE_ID,
            "diffusers_gguf_variant": WAI_GGUF_VARIANT_ID,
            "diffusers_gguf_file": WAI_GGUF_FILE,
            "diffusers_model_variant": f"gguf::{WAI_GGUF_FILE}",
            "prompt": "illustration",
            "negative_prompt": "",
            "sampler_name": "diffusers-auto",
            "scheduler": "default",
            "seed": 123,
            "backend_url": "diffusers://frontend",
            "confirm_billing": True,
        },
    )

    payload = _await_comfyui_result(client, generated)
    installed = comfy_base / "models" / "unet" / WAI_GGUF_FILE
    assert installed.read_bytes() == b"fake gguf"
    assert payload["image"]["image_ref"]["filename"] == "hackme_web_00001_.png"
    assert FakeComfyUIClient.last_params["backend_kind"] == "comfyui_gguf"
    assert FakeComfyUIClient.last_params["model"] == WAI_GGUF_FILE


def test_comfyui_diffusers_mode_remote_native_gguf_requires_remote_admin(tmp_path):
    cached_gguf = tmp_path / "hf-cache" / WAI_GGUF_FILE
    cached_gguf.parent.mkdir(parents=True)
    cached_gguf.write_bytes(b"fake gguf")

    class AutoRouteDiffusersBackendClient(FakeDiffusersBackendClient):
        def prepare_gguf_file_for_backend(self, model_repo, gguf_file, *, progress_callback=None, log_capture=None):
            return {
                "path": str(cached_gguf),
                "stats": {"cache_hit": True, "bytes_written": cached_gguf.stat().st_size, "total_bytes": cached_gguf.stat().st_size},
                "metadata": {"has_comfy_metadata": True, "has_original_unet_names": True},
                "suggested_backend": "comfyui_gguf",
                "model_repo": model_repo,
                "gguf_file": gguf_file,
            }

    class RemoteMissingGgufComfyUIClient(FakeNativeGgufComfyUIClient):
        def get_capabilities(self):
            payload = super().get_capabilities()
            payload["diffusion_models"] = []
            return payload

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)

    def factory(url):
        if str(url).startswith("diffusers://"):
            return AutoRouteDiffusersBackendClient()
        return RemoteMissingGgufComfyUIClient()

    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": WAI_GGUF_REPO,
            "comfyui_remote_api_url": "http://127.0.0.1:8188",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": factory,
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": WAI_GGUF_REPO,
            "diffusers_model_repo": WAI_GGUF_REPO,
            "diffusers_gguf_profile": WAI_GGUF_PROFILE_ID,
            "diffusers_gguf_variant": WAI_GGUF_VARIANT_ID,
            "diffusers_gguf_file": WAI_GGUF_FILE,
            "diffusers_model_variant": f"gguf::{WAI_GGUF_FILE}",
            "prompt": "illustration",
            "negative_prompt": "",
            "sampler_name": "diffusers-auto",
            "scheduler": "default",
            "seed": 123,
            "confirm_billing": True,
        },
    )

    job = _await_comfyui_job(client, generated, expected_status="error")
    assert "遠端 ComfyUI API 無法由本站直接寫入模型檔" in job["error"]
    assert "請聯絡遠端 ComfyUI 管理人" in job["error"]
    assert f"models/unet/{WAI_GGUF_FILE}" in job["error"]


def test_comfyui_diffusers_stale_progress_does_not_say_comfyui_backend(tmp_path, monkeypatch):
    stale_progress_ready = threading.Event()

    class SlowDiffusersBackendClient(FakeDiffusersBackendClient):
        def generate_image(self, params, *, timeout_seconds=180, progress_callback=None, extra_data=None):
            if progress_callback:
                progress_callback({
                    "phase": "downloading",
                    "percent": 5,
                    "backend_kind": "diffusers",
                    "step": "Hugging Face 檔案下載",
                    "current_file": "Downloading (incomplete total...)",
                    "detail": "下載 Diffusers model：dhead/waiIllustriousSDXL_v150",
                    "cache_hit": True,
                    "bytes_written": 0,
                    "total_bytes": 0,
                    "python_log_tail": ["Download complete: dhead/waiIllustriousSDXL_v150 (cache hit; no network bytes reported)"],
                })
                stale_progress_ready.set()
            time.sleep(0.25)
            return super().generate_image(params, timeout_seconds=timeout_seconds, progress_callback=progress_callback, extra_data=extra_data)

    monkeypatch.setattr(comfyui_routes, "COMFYUI_JOB_STALE_SECONDS", 0)
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": "dhead/waiIllustriousSDXL_v150",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: SlowDiffusersBackendClient(),
        },
    ).test_client()

    started = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": "dhead/waiIllustriousSDXL_v150",
            "prompt": "illustration",
            "sampler_name": "diffusers-auto",
            "scheduler": "default",
            "seed": 123,
            "confirm_billing": True,
        },
    )
    assert started.status_code == 200, started.get_json()
    job_id = started.get_json()["job"]["job_id"]
    assert stale_progress_ready.wait(10.0)

    polled = client.get(f"/api/comfyui/jobs/{job_id}")
    assert polled.status_code == 200
    progress = polled.get_json()["job"]["progress"]
    assert progress["backend_unresponsive"] is True
    assert progress["backend_kind"] == "diffusers"
    assert progress["phase"] == "backend_unresponsive"
    assert "Diffusers / Hugging Face 暫時沒有回報新進度" in progress["detail"]
    assert "本機 cache" in progress["detail"]
    assert "網路下載位元組" in progress["detail"]
    assert "下載大型模型" not in progress["detail"]
    assert "ComfyUI 後端" not in progress["detail"]


def test_comfyui_diffusers_failure_reports_reason_and_python_log_tail(tmp_path):
    class FailingDiffusersBackendClient(FakeDiffusersBackendClient):
        def generate_image(self, params, *, timeout_seconds=180, progress_callback=None, extra_data=None):
            if progress_callback:
                progress_callback({
                    "phase": "running",
                    "percent": 40,
                    "backend_kind": "diffusers",
                    "detail": "Diffusers 推論啟動",
                    "python_log_tail": ["diffusers pipeline loading", "diffusers inference failed"],
                })
            raise ComfyUIError("Diffusers exploded: missing tensor")

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": "dhead/waiIllustriousSDXL_v150",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: FailingDiffusersBackendClient(),
        },
    ).test_client()

    started = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "model": "dhead/waiIllustriousSDXL_v150",
            "prompt": "illustration",
            "sampler_name": "diffusers-auto",
            "scheduler": "default",
            "seed": 123,
            "confirm_billing": True,
        },
    )

    job = _await_comfyui_job(client, started, expected_status="error")
    progress = job["progress"]
    assert "Diffusers exploded: missing tensor" in job["error"]
    assert progress["backend_kind"] == "diffusers"
    assert progress["phase"] == "error"
    assert progress["error_message"] == "Diffusers exploded: missing tensor"
    assert progress["step"] == "Diffusers 產圖失敗"
    assert progress["python_log_tail"] == ["diffusers pipeline loading", "diffusers inference failed"]


def test_comfyui_diffusers_mode_allows_generation_page_repo_override(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": "dhead/waiIllustriousSDXL_v150",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: FakeDiffusersBackendClient(),
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
        json={
            "generation_mode": "txt2img",
            "diffusers_model_repo": "https://huggingface.co/hf-internal-testing/tiny-sdxl-pipe/tree/main",
            "prompt": "illustration",
            "sampler_name": "diffusers-auto",
            "scheduler": "default",
            "confirm_billing": True,
        },
    )

    assert generated.status_code == 200, generated.get_json()
    _await_comfyui_result(client, generated)
    assert FakeDiffusersBackendClient.last_params["model"] == "hf-internal-testing/tiny-sdxl-pipe"
    assert FakeDiffusersBackendClient.last_params["diffusers_model_repo"] == "hf-internal-testing/tiny-sdxl-pipe"


def test_comfyui_diffusers_mode_rejects_comfyui_controlnet_shortcut(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "diffusers",
            "comfyui_diffusers_model_repo": "dhead/waiIllustriousSDXL_v150",
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: FakeDiffusersBackendClient(),
        },
    ).test_client()

    generated = client.post(
        "/api/comfyui/generate",
            json={
                "generation_mode": "txt2img",
                "model": "dhead/waiIllustriousSDXL_v150",
                "prompt": "illustration",
                "controlnet_enabled": True,
                "controlnet_type": "canny",
                "control_strength": 1.0,
                "control_start": 0.0,
                "control_end": 1.0,
                "confirm_billing": True,
            },
        )

    assert generated.status_code == 409
    assert "Diffusers 後端目前不支援本站 ControlNet" in generated.get_json()["msg"]


def test_root_can_download_local_comfyui_start_template(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(db_path, storage_root, actor=lambda: root_actor).test_client()

    response = client.get("/api/root/comfyui/local-start-template")

    assert response.status_code == 200
    assert "attachment" in (response.headers.get("Content-Disposition") or "")
    payload = response.get_data(as_text=True)
    assert "COMFYUI_ROOT" in payload
    assert 'main.py --listen "$LISTEN_HOST" --port "$LISTEN_PORT" "${EXTRA_ARGS[@]}"' in payload
    assert "COMFYUI_EXTRA_ARGS" in payload


def test_comfyui_local_start_template_download_requires_root(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    forbidden = client.get("/api/root/comfyui/local-start-template")

    assert forbidden.status_code == 403


def test_local_comfyui_start_reuses_existing_backend(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    comfy_base = tmp_path / "ComfyUI_windows_portable"
    storage_root.mkdir()
    comfy_base.mkdir()
    (comfy_base / "run_in_linux.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
            "comfyui_local_start_script": "run_in_linux.sh",
        },
    ).test_client()

    started = client.post("/api/comfyui/start", json={})

    assert started.status_code == 200
    body = started.get_json()
    assert body["ok"] is True
    assert body["connection_mode"] == "local"
    assert body["start"]["already_running"] is True


def test_local_comfyui_status_reports_starting_when_process_alive(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    comfy_base = tmp_path / "ComfyUI_windows_portable"
    storage_root.mkdir()
    comfy_base.mkdir()
    _init_db(db_path)
    temp_root = tmp_path / "tmp-runtime"
    temp_root.mkdir()
    port = 8192
    log_path = temp_root / "comfy-start.log"
    log_path.write_text(
        "Starting server\nTo see the GUI go to: http://0.0.0.0:8192\nFETCH ComfyRegistry Data: 40/143\n",
        encoding="utf-8",
    )
    state_file = temp_root / f"hackme_web_comfyui_local_{port}.json"
    state_file.write_text(
        '{"pid": 4321, "pgid": 4321, "port": 8192, "base_dir": "%s", "script": "run_in_linux.sh", "log_path": "%s"}'
        % (str(comfy_base), str(log_path)),
        encoding="utf-8",
    )
    monkeypatch.setattr("routes.comfyui.tempfile.gettempdir", lambda: str(temp_root))

    def fake_kill(pid, sig):
        if int(pid) == 4321 and sig == 0:
            return None
        raise ProcessLookupError()

    monkeypatch.setattr("routes.comfyui.os.kill", fake_kill)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
            "comfyui_api_host": "localhost",
            "comfyui_api_port": port,
        },
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: OfflineComfyUIClient(),
        },
    ).test_client()

    status = client.get("/api/comfyui/status")

    assert status.status_code == 200
    body = status.get_json()
    assert body["ok"] is True
    assert body["available"] is False
    assert body["starting"] is True
    assert "正在載入自訂節點 / Registry" in body["msg"]
    assert body["startup_log_tail"][-1] == "FETCH ComfyRegistry Data: 40/143"


def test_comfyui_connection_test_requires_root_and_valid_endpoint(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    user_client = _build_app(db_path, storage_root).test_client()

    forbidden = user_client.post("/api/root/comfyui/test-connection", json={"host": "localhost", "port": 8192})
    assert forbidden.status_code == 403

    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    root_client = _build_app(db_path, storage_root, actor=lambda: root_actor).test_client()
    invalid = root_client.post("/api/root/comfyui/test-connection", json={"host": "http://127.0.0.1/path", "port": 8192})
    assert invalid.status_code == 400
    assert "Host" in invalid.get_json()["msg"]


def test_comfyui_connection_test_preserves_remote_api_url_error_messages(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(db_path, storage_root, actor=lambda: root_actor).test_client()

    credentials = client.post(
        "/api/root/comfyui/test-connection",
        json={"mode": "remote", "api_url": "http://user:pass@127.0.0.1:8192"},
    )
    assert credentials.status_code == 400
    assert credentials.get_json()["msg"] == "ComfyUI API 位址不可包含帳密"

    with_path = client.post(
        "/api/root/comfyui/test-connection",
        json={"mode": "remote", "api_url": "https://127.0.0.1:8192/prompt"},
    )
    assert with_path.status_code == 400
    assert with_path.get_json()["msg"] == "ComfyUI API 位址只需填主機與 port，不要包含路徑或參數"


def test_local_comfyui_connection_test_attempts_autostart(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    comfy_base = tmp_path / "ComfyUI_windows_portable"
    storage_root.mkdir()
    comfy_base.mkdir()
    (comfy_base / "run_in_linux.sh").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    _init_db(db_path)
    state = {"ready": False, "popen_args": None, "popen_kwargs": None}

    class DummyPopen:
        def __init__(self, *args, **kwargs):
            self.pid = 4321
            state["ready"] = True
            state["popen_args"] = args
            state["popen_kwargs"] = kwargs

        def poll(self):
            return None

    class DummyRunResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("routes.comfyui.subprocess.run", lambda *args, **kwargs: DummyRunResult())
    monkeypatch.setattr("routes.comfyui.subprocess.Popen", DummyPopen)
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
            "comfyui_local_start_script": "run_in_linux.sh",
            "comfyui_api_host": "localhost",
            "comfyui_api_port": 8192,
            "comfyui_local_vram_mode": "lowvram",
            "comfyui_local_precision": "force_fp16",
            "comfyui_local_unet_dtype": "fp8_e4m3fn",
            "comfyui_local_cpu_vae": True,
            "comfyui_local_attention_mode": "pytorch",
            "comfyui_local_reserve_vram_gb": "1.5",
        },
        actor=lambda: root_actor,
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: RecoveringComfyUIClient(state),
        },
    ).test_client()

    tested = client.post(
        "/api/root/comfyui/test-connection",
        json={
            "mode": "local",
            "host": "localhost",
            "port": 8192,
            "base_dir": str(comfy_base),
            "local_start_script": "run_in_linux.sh",
        },
    )

    assert tested.status_code == 200
    body = tested.get_json()
    assert body["ok"] is True
    assert body["available"] is True
    assert body["autostart"]["attempted"] is True
    assert body["autostart"]["start"]["started"] is True
    command = state["popen_args"][0]
    assert command[:2] == ["bash", str(comfy_base / "run_in_linux.sh")]
    assert "--lowvram" in command
    assert "--force-fp16" in command
    assert "--fp8_e4m3fn-unet" in command
    assert "--cpu-vae" in command
    assert "--use-pytorch-cross-attention" in command
    assert "--reserve-vram" in command
    assert "1.5" in command
    env = state["popen_kwargs"]["env"]
    assert "--lowvram" in env["COMFYUI_EXTRA_ARGS"]
    assert "--fp8_e4m3fn-unet" in env["COMFYUI_EXTRA_ARGS_JSON"]


def test_local_comfyui_connection_test_reports_startup_failure_detail(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    comfy_base = tmp_path / "ComfyUI_windows_portable"
    storage_root.mkdir()
    comfy_base.mkdir()
    (comfy_base / "run_in_linux.sh").write_text("#!/usr/bin/env bash\necho boot failed >&2\nexit 3\n", encoding="utf-8")
    _init_db(db_path)

    class DummyPopen:
        def __init__(self, *args, **kwargs):
            self.pid = 4321
            self._returncode = 3

        def poll(self):
            return self._returncode

    class DummyRunResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("routes.comfyui.subprocess.run", lambda *args, **kwargs: DummyRunResult())
    monkeypatch.setattr("routes.comfyui.subprocess.Popen", DummyPopen)
    monkeypatch.setattr("routes.comfyui.time.sleep", lambda *_args, **_kwargs: None)
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
            "comfyui_local_start_script": "run_in_linux.sh",
            "comfyui_api_host": "localhost",
            "comfyui_api_port": 8192,
        },
        actor=lambda: root_actor,
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: OfflineComfyUIClient(),
        },
    ).test_client()

    tested = client.post(
        "/api/root/comfyui/test-connection",
        json={
            "mode": "local",
            "host": "localhost",
            "port": 8192,
            "base_dir": str(comfy_base),
            "local_start_script": "run_in_linux.sh",
        },
    )

    assert tested.status_code == 200
    body = tested.get_json()
    assert body["ok"] is True
    assert body["available"] is False
    assert body["autostart"]["attempted"] is True
    assert "exit 3" in body["autostart"]["message"]


def test_root_can_stop_local_comfyui_with_tracked_pid(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    comfy_base = tmp_path / "ComfyUI_windows_portable"
    storage_root.mkdir()
    comfy_base.mkdir()
    _init_db(db_path)
    state = {"ready": True}
    temp_root = tmp_path / "tmp-runtime"
    temp_root.mkdir()
    port = 8192
    state_file = temp_root / f"hackme_web_comfyui_local_{port}.json"
    state_file.write_text(
        '{"pid": 4321, "pgid": 4321, "port": 8192, "base_dir": "%s", "script": "run_in_linux.sh"}' % str(comfy_base),
        encoding="utf-8",
    )
    monkeypatch.setattr("routes.comfyui.tempfile.gettempdir", lambda: str(temp_root))
    monkeypatch.setattr("routes.comfyui.time.sleep", lambda *_args, **_kwargs: None)

    def fake_killpg(pgid, sig):
        assert pgid == 4321
        state["ready"] = False

    def fake_kill(pid, sig):
        if int(pid) != 4321:
            raise ProcessLookupError()
        if sig == 0:
            if state["ready"]:
                return None
            raise ProcessLookupError()
        return None

    monkeypatch.setattr("routes.comfyui.os.killpg", fake_killpg)
    monkeypatch.setattr("routes.comfyui.os.kill", fake_kill)

    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
            "comfyui_local_start_script": "run_in_linux.sh",
            "comfyui_api_host": "localhost",
            "comfyui_api_port": port,
        },
        actor=lambda: root_actor,
        extra_deps={
            "comfyui_client": None,
            "comfyui_client_factory": lambda url: RecoveringComfyUIClient(state),
        },
    ).test_client()

    stopped = client.post("/api/root/comfyui/stop", json={})

    assert stopped.status_code == 200
    body = stopped.get_json()
    assert body["ok"] is True
    assert body["stop"]["stopped"] is True
    assert body["stop"]["killed_pids"] == [4321]
    assert not state_file.exists()


def test_root_comfyui_stop_requires_root(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    comfy_base = tmp_path / "ComfyUI_windows_portable"
    storage_root.mkdir()
    comfy_base.mkdir()
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
            "comfyui_api_host": "localhost",
            "comfyui_api_port": 8192,
        },
    ).test_client()

    response = client.post("/api/root/comfyui/stop", json={})

    assert response.status_code == 403


def test_comfyui_civitai_inspect_requires_root_and_civitai_url(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    user_client = _build_app(db_path, storage_root).test_client()

    forbidden = user_client.post("/api/root/comfyui/civitai/inspect", json={"page_url": "https://civitai.com/models/123/a"})
    assert forbidden.status_code == 403

    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    root_client = _build_app(db_path, storage_root, actor=lambda: root_actor).test_client()
    invalid = root_client.post(
        "/api/root/comfyui/civitai/inspect",
        json={"page_url": "http://127.0.0.1/models/123456/local"},
    )
    assert invalid.status_code == 400
    assert "Civitai" in invalid.get_json()["msg"]


def test_comfyui_civitai_search_requires_root_and_reports_missing_api_key(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    user_client = _build_app(db_path, storage_root).test_client()

    forbidden = user_client.post("/api/root/comfyui/civitai/search", json={"query": "anime"})
    assert forbidden.status_code == 403

    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    root_client = _build_app(db_path, storage_root, actor=lambda: root_actor).test_client()
    missing_key = root_client.post(
        "/api/root/comfyui/civitai/search",
        json={"query": "anime", "model_type": "checkpoint", "base_model": "SDXL", "nsfw_mode": "safe"},
    )
    assert missing_key.status_code == 400
    assert "Civitai API Key" in missing_key.get_json()["msg"]


def test_comfyui_civitai_search_returns_filtered_results_and_audit(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    audit_events = []
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={"comfyui_civitai_api_key": "secret-token"},
        extra_deps={"audit": lambda *args, **kwargs: audit_events.append((args, kwargs))},
    ).test_client()

    def fake_urlopen(request_obj, timeout=0):
        url = request_obj.full_url
        headers = dict(request_obj.header_items())
        parsed = urllib.parse.urlsplit(url)
        if parsed.netloc == "image.civitai.com":
            assert parsed.path == "/xG1nkqKTMzGDvpLrqFT7WA/knight.jpeg"
            return _FakeResponse(url=url, body=b"\xff\xd8\xff\xe0fakejpeg", headers={"Content-Type": "image/jpeg"})
        query = urllib.parse.parse_qs(parsed.query)
        assert parsed.scheme == "https"
        assert parsed.netloc == "civitai.com"
        assert parsed.path == "/api/v1/models"
        assert headers.get("Authorization") == "Bearer secret-token"
        assert query.get("query") == ["anime knight"]
        assert query.get("types") == ["Checkpoint"]
        assert query.get("baseModels") == ["SDXL"]
        assert query.get("nsfw") == ["false"]
        payload = {
            "items": [
                {
                    "id": 654321,
                    "name": "Knight XL",
                    "type": "Checkpoint",
                    "creator": {"username": "artist"},
                    "nsfw": False,
                    "modelVersions": [
                        {
                            "id": 9001,
                            "name": "v3",
                            "baseModel": "SDXL",
                            "createdAt": "2026-05-01T12:00:00Z",
                            "trainedWords": ["knight armor"],
                            "images": [
                                {
                                    "url": "https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA/knight.jpeg",
                                    "nsfw": "None",
                                    "width": 768,
                                    "height": 1024,
                                    "hash": "thumbhash",
                                },
                                {
                                    "url": "javascript:alert(1)",
                                    "width": 512,
                                    "height": 512,
                                },
                            ],
                            "files": [
                                {
                                    "id": 9101,
                                    "name": "knight_xl_v3.safetensors",
                                    "sizeKB": 4096,
                                    "hashes": {"SHA256": "deadbeef"},
                                    "type": "Model",
                                    "downloadUrl": "https://civitai.com/api/download/models/9101",
                                }
                            ],
                        }
                    ],
                },
                {
                    "id": 654322,
                    "name": "Wrong LoRA",
                    "type": "LORA",
                    "creator": {"username": "artist"},
                    "nsfw": False,
                    "modelVersions": [
                        {
                            "id": 9002,
                            "name": "v1",
                            "baseModel": "SDXL",
                            "createdAt": "2026-05-02T12:00:00Z",
                            "files": [
                                {
                                    "id": 9102,
                                    "name": "wrong_lora.safetensors",
                                    "sizeKB": 1024,
                                    "hashes": {"SHA256": "badcafe"},
                                    "type": "Model",
                                    "downloadUrl": "https://civitai.com/api/download/models/9102",
                                }
                            ],
                        }
                    ],
                }
            ],
            "metadata": {"totalItems": 2, "currentPage": 1, "pageSize": 12},
        }
        return _FakeResponse(url=url, body=json.dumps(payload), headers={"Content-Type": "application/json; charset=utf-8"})

    monkeypatch.setattr(comfyui_routes.urllib.request, "urlopen", fake_urlopen)

    searched = client.post(
        "/api/root/comfyui/civitai/search",
        json={"query": "anime knight", "model_type": "checkpoint", "base_model": "SDXL", "nsfw_mode": "safe"},
    )
    assert searched.status_code == 200
    body = searched.get_json()
    assert body["ok"] is True
    assert body["total_items"] == 1
    assert len(body["results"]) == 1
    assert body["results"][0]["model_id"] == 654321
    assert body["results"][0]["selected_page_url"].endswith("654321?modelVersionId=9001")
    assert body["results"][0]["compatible_models"] == ["SDXL"]
    assert body["results"][0]["thumbnail_url"] == "https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA/knight.jpeg"
    assert body["results"][0]["thumbnail_proxy_url"].startswith("/api/root/comfyui/civitai/media?url=")
    assert body["results"][0]["latest_version"]["thumbnail_url"] == body["results"][0]["thumbnail_url"]
    assert body["results"][0]["latest_version"]["thumbnail_proxy_url"] == body["results"][0]["thumbnail_proxy_url"]
    assert body["results"][0]["latest_version"]["images"] == [
        {
            "url": "https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA/knight.jpeg",
            "proxy_url": body["results"][0]["thumbnail_proxy_url"],
            "nsfw": "None",
            "width": 768,
            "height": 1024,
            "hash": "thumbhash",
        }
    ]
    assert body["results"][0]["latest_version"]["primary_file"]["hashes"]["sha256"] == "deadbeef"
    assert body["results"][0]["suggested_model_type"] == "checkpoint"
    assert audit_events
    assert audit_events[-1][0][0] == "COMFYUI_CIVITAI_SEARCH"

    proxied = client.get(
        "/api/root/comfyui/civitai/media",
        query_string={"url": "https://image.civitai.com/xG1nkqKTMzGDvpLrqFT7WA/knight.jpeg"},
    )
    assert proxied.status_code == 200
    assert proxied.headers["Content-Type"].startswith("image/jpeg")


def test_comfyui_civitai_red_url_is_accepted(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={"comfyui_civitai_api_key": "secret-token"},
    ).test_client()

    def fake_urlopen(request_obj, timeout=0):
        url = request_obj.full_url
        if url == "https://civitai.red/api/v1/models/376130":
            payload = {
                "id": 376130,
                "name": "novaAnimeIL_V18",
                "type": "Checkpoint",
                "creator": {"username": "demo"},
                "modelVersions": [
                    {
                        "id": 2837020,
                        "name": "v18",
                        "trainedWords": ["masterpiece", "1girl"],
                        "downloadUrl": "https://civitai.com/api/download/models/2837020",
                        "files": [
                            {
                                "id": 5001,
                                "name": "novaAnimeIL_V18.safetensors",
                                "sizeKB": 1024,
                                "downloadUrl": "https://civitai.com/api/download/models/2837020",
                            }
                        ],
                    }
                ],
            }
            return _FakeResponse(url=url, body=json.dumps(payload), headers={"Content-Type": "application/json; charset=utf-8"})
        raise AssertionError(f"unexpected urlopen target: {url}")

    monkeypatch.setattr(comfyui_routes.urllib.request, "urlopen", fake_urlopen)
    inspected = client.post(
        "/api/root/comfyui/civitai/inspect",
        json={"page_url": "https://civitai.red/models/376130?modelVersionId=2837020"},
    )

    assert inspected.status_code == 200
    body = inspected.get_json()
    assert body["model"]["model_id"] == 376130
    assert body["model"]["source_site"] == "civitai.red"
    assert body["model"]["selected_version_id"] == 2837020
    assert body["model"]["versions"][0]["trained_words"] == ["masterpiece", "1girl"]


def test_comfyui_civitai_search_queries_com_and_red_sources(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={"comfyui_civitai_api_key": "secret-token"},
    ).test_client()
    seen_hosts = []

    def fake_urlopen(request_obj, timeout=0):
        url = request_obj.full_url
        headers = dict(request_obj.header_items())
        parsed = urllib.parse.urlsplit(url)
        seen_hosts.append(parsed.netloc)
        assert parsed.scheme == "https"
        assert parsed.path == "/api/v1/models"
        assert headers.get("Authorization") == "Bearer secret-token"
        query = urllib.parse.parse_qs(parsed.query)
        assert query.get("query") == ["anime"]
        assert query.get("types") == ["Checkpoint"]
        if parsed.netloc == "civitai.com":
            payload = {
                "items": [
                    {
                        "id": 111,
                        "name": "Com Checkpoint",
                        "type": "Checkpoint",
                        "creator": {"username": "com-user"},
                        "modelVersions": [
                            {
                                "id": 1001,
                                "name": "com-v1",
                                "baseModel": "SDXL",
                                "files": [{"id": 2001, "name": "com.safetensors", "downloadUrl": "https://civitai.com/api/download/models/1001"}],
                            }
                        ],
                    }
                ],
                "metadata": {"totalItems": 1, "currentPage": 1, "pageSize": 12},
            }
            return _FakeResponse(url=url, body=json.dumps(payload), headers={"Content-Type": "application/json; charset=utf-8"})
        if parsed.netloc == "civitai.red":
            payload = {
                "items": [
                    {
                        "id": 222,
                        "name": "Red Checkpoint",
                        "type": "Checkpoint",
                        "creator": {"username": "red-user"},
                        "modelVersions": [
                            {
                                "id": 1002,
                                "name": "red-v1",
                                "baseModel": "Pony",
                                "files": [{"id": 2002, "name": "red.safetensors", "downloadUrl": "https://civitai.red/api/download/models/1002"}],
                            }
                        ],
                    },
                    {
                        "id": 333,
                        "name": "Wrong Red LoRA",
                        "type": "LORA",
                        "creator": {"username": "red-user"},
                        "modelVersions": [
                            {
                                "id": 1003,
                                "name": "red-lora",
                                "baseModel": "SDXL",
                                "files": [{"id": 2003, "name": "red_lora.safetensors", "downloadUrl": "https://civitai.red/api/download/models/1003"}],
                            }
                        ],
                    },
                ],
                "metadata": {"totalItems": 2, "currentPage": 1, "pageSize": 12},
            }
            return _FakeResponse(url=url, body=json.dumps(payload), headers={"Content-Type": "application/json; charset=utf-8"})
        raise AssertionError(f"unexpected urlopen target: {url}")

    monkeypatch.setattr(comfyui_routes.urllib.request, "urlopen", fake_urlopen)
    searched = client.post(
        "/api/root/comfyui/civitai/search",
        json={"query": "anime", "model_type": "checkpoint", "nsfw_mode": "all"},
    )
    assert searched.status_code == 200
    body = searched.get_json()
    assert body["ok"] is True
    assert seen_hosts.count("civitai.com") == 1
    assert seen_hosts.count("civitai.red") == 1
    assert body["source_errors"] == []
    assert [item["source_site"] for item in body["search_sources"]] == ["civitai.com", "civitai.red"]
    assert [item["source_site"] for item in body["results"]] == ["civitai.com", "civitai.red"]
    assert [item["suggested_model_type"] for item in body["results"]] == ["checkpoint", "checkpoint"]
    assert body["results"][0]["select_key"] == "civitai.com:111"
    assert body["results"][1]["select_key"] == "civitai.red:222"
    assert body["results"][1]["selected_page_url"] == "https://civitai.red/models/222?modelVersionId=1002"


def test_comfyui_civitai_inspect_and_download_flow(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    comfy_base.mkdir()
    (comfy_base / "main.py").write_text("# comfy", encoding="utf-8")
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={
            "comfyui_civitai_api_key": "secret-token",
            "comfyui_base_dir": str(comfy_base),
        },
    ).test_client()

    def fake_urlopen(request_obj, timeout=0):
        url = request_obj.full_url
        headers = dict(request_obj.header_items())
        if url == "https://civitai.com/api/v1/models/123456":
            assert headers.get("Authorization") == "Bearer secret-token"
            payload = {
                "id": 123456,
                "name": "Fancy Model",
                "type": "LORA",
                "creator": {"username": "artist"},
                "modelVersions": [
                    {
                        "id": 2001,
                        "name": "v1",
                        "baseModel": "SDXL",
                        "trainedWords": ["fancy style"],
                        "downloadUrl": "https://civitai.com/api/download/models/2001",
                        "files": [
                            {
                                "id": 3001,
                                "name": "fancy_v1.safetensors",
                                "sizeKB": 2048,
                                "downloadUrl": "https://civitai.com/api/download/models/2001",
                                "type": "Model",
                            }
                        ],
                    },
                    {
                        "id": 2002,
                        "name": "v2",
                        "baseModel": "SDXL",
                        "trainedWords": ["fancy style", "cinematic"],
                        "downloadUrl": "https://civitai.com/api/download/models/2002",
                        "files": [
                            {
                                "id": 3002,
                                "name": "fancy_v2.safetensors",
                                "sizeKB": 4096,
                                "downloadUrl": "https://civitai.com/api/download/models/2002",
                                "type": "Model",
                            }
                        ],
                    },
                ],
            }
            return _FakeResponse(url=url, body=json.dumps(payload), headers={"Content-Type": "application/json; charset=utf-8"})
        if url.startswith("https://civitai.com/api/download/models/2002"):
            parsed = urllib.parse.urlsplit(url)
            assert urllib.parse.parse_qs(parsed.query).get("token") == ["secret-token"]
            return _FakeResponse(
                url=url,
                body=b"fake-model-bytes",
                headers={"Content-Disposition": 'attachment; filename="fancy_v2.safetensors"'},
            )
        raise AssertionError(f"unexpected urlopen target: {url}")

    monkeypatch.setattr(comfyui_routes.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        comfyui_routes.socket,
        "getaddrinfo",
        lambda host, port, type=0: [(None, None, None, None, ("104.21.72.187", port or 443))],
    )

    inspected = client.post(
        "/api/root/comfyui/civitai/inspect",
        json={"page_url": "https://civitai.com/models/123456/fancy-model?modelVersionId=2002"},
    )
    assert inspected.status_code == 200
    inspected_json = inspected.get_json()
    assert inspected_json["model"]["name"] == "Fancy Model"
    assert inspected_json["model"]["selected_version_id"] == 2002
    assert inspected_json["model"]["suggested_model_type"] == "lora"
    assert inspected_json["model"]["versions"][1]["files"][0]["id"] == 3002
    assert inspected_json["model"]["versions"][1]["trained_words"] == ["fancy style", "cinematic"]

    downloaded = client.post(
        "/api/root/comfyui/civitai/download",
        json={
            "page_url": "https://civitai.com/models/123456/fancy-model?modelVersionId=2002",
            "version_id": 2002,
            "file_id": 3002,
            "type": "lora",
            "base_dir": str(comfy_base),
        },
    )
    assert downloaded.status_code == 200
    downloaded_json = downloaded.get_json()
    assert downloaded_json["download"]["filename"] == "fancy_v2.safetensors"
    assert downloaded_json["download"]["civitai"]["version_id"] == 2002
    assert downloaded_json["download"]["civitai"]["base_model"] == "SDXL"
    assert downloaded_json["download"]["civitai"]["trained_words"] == ["fancy style", "cinematic"]
    assert (comfy_base / "models" / "loras" / "fancy_v2.safetensors").exists()
    sidecar = comfy_base / "models" / "loras" / "fancy_v2.safetensors.civitai.json"
    assert sidecar.exists()
    sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sidecar_data["base_model"] == "SDXL"
    assert sidecar_data["trained_words"] == ["fancy style", "cinematic"]
    assert sidecar_data["source"] == "civitai"

    metadata_client = _build_app(
        db_path,
        storage_root,
        settings={"comfyui_base_dir": str(comfy_base)},
        comfyui_client=LocalLoraMetadataClient(),
    ).test_client()
    listed = metadata_client.get("/api/comfyui/models")
    assert listed.status_code == 200
    listed_json = listed.get_json()
    assert listed_json["lora_details"]["fancy_v2.safetensors"]["base_model"] == "SDXL"
    assert listed_json["lora_details"]["fancy_v2.safetensors"]["supported"] is True
    assert listed_json["lora_details"]["fancy_v2.safetensors"]["trained_words"] == ["fancy style", "cinematic"]


def test_comfyui_civitai_download_reports_interrupted_transfer(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    comfy_base.mkdir()
    (comfy_base / "main.py").write_text("# comfy", encoding="utf-8")
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={
            "comfyui_civitai_api_key": "secret-token",
            "comfyui_base_dir": str(comfy_base),
        },
    ).test_client()

    def fake_urlopen(request_obj, timeout=0):
        url = request_obj.full_url
        if url == "https://civitai.com/api/v1/models/123456":
            payload = {
                "id": 123456,
                "name": "Fancy Model",
                "type": "Controlnet",
                "creator": {"username": "artist"},
                "modelVersions": [
                    {
                        "id": 2002,
                        "name": "v2",
                        "baseModel": "SDXL",
                        "files": [
                            {
                                "id": 3002,
                                "name": "fancy_controlnet_v2.safetensors",
                                "sizeKB": 4096,
                                "downloadUrl": "https://civitai.com/api/download/models/2002",
                                "type": "Model",
                            }
                        ],
                    },
                ],
            }
            return _FakeResponse(url=url, body=json.dumps(payload), headers={"Content-Type": "application/json; charset=utf-8"})
        if url.startswith("https://civitai.com/api/download/models/2002"):
            raise comfyui_routes.urllib.error.URLError("connection reset by peer")
        raise AssertionError(f"unexpected urlopen target: {url}")

    monkeypatch.setattr(comfyui_routes.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        comfyui_routes.socket,
        "getaddrinfo",
        lambda host, port, type=0: [(None, None, None, None, ("104.21.72.187", port or 443))],
    )

    downloaded = client.post(
        "/api/root/comfyui/civitai/download",
        json={
            "page_url": "https://civitai.com/models/123456/fancy-model?modelVersionId=2002",
            "version_id": 2002,
            "file_id": 3002,
            "type": "controlnet",
            "base_dir": str(comfy_base),
        },
    )
    assert downloaded.status_code == 400
    assert "下載中斷或連線失敗" in downloaded.get_json()["msg"]


def test_comfyui_civitai_download_supports_upscale_default_dir(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    comfy_base.mkdir()
    (comfy_base / "main.py").write_text("# comfy", encoding="utf-8")
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={
            "comfyui_civitai_api_key": "secret-token",
            "comfyui_base_dir": str(comfy_base),
        },
    ).test_client()

    def fake_urlopen(request_obj, timeout=0):
        url = request_obj.full_url
        if url == "https://civitai.com/api/v1/models/55555":
            payload = {
                "id": 55555,
                "name": "Ultra Upscaler",
                "type": "Upscaler",
                "creator": {"username": "artist"},
                "modelVersions": [
                    {
                        "id": 6001,
                        "name": "v1",
                        "baseModel": "SDXL",
                        "files": [
                            {
                                "id": 7001,
                                "name": "4x-UltraSharp.pth",
                                "sizeKB": 1024,
                                "downloadUrl": "https://civitai.com/api/download/models/6001",
                                "type": "Model",
                            }
                        ],
                    },
                ],
            }
            return _FakeResponse(url=url, body=json.dumps(payload), headers={"Content-Type": "application/json; charset=utf-8"})
        if url.startswith("https://civitai.com/api/download/models/6001"):
            return _FakeResponse(
                url=url,
                body=b"fake-upscale-bytes",
                headers={"Content-Disposition": 'attachment; filename="4x-UltraSharp.pth"'},
            )
        raise AssertionError(f"unexpected urlopen target: {url}")

    monkeypatch.setattr(comfyui_routes.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        comfyui_routes.socket,
        "getaddrinfo",
        lambda host, port, type=0: [(None, None, None, None, ("104.21.72.187", port or 443))],
    )

    downloaded = client.post(
        "/api/root/comfyui/civitai/download",
        json={
            "page_url": "https://civitai.com/models/55555/upscale?modelVersionId=6001",
            "version_id": 6001,
            "file_id": 7001,
            "type": "upscale",
            "base_dir": str(comfy_base),
        },
    )

    assert downloaded.status_code == 200
    body = downloaded.get_json()
    assert body["download"]["relative_dir"] == "upscale_models"
    saved = comfy_base / "models" / "upscale_models" / "4x-UltraSharp.pth"
    assert saved.exists()
    sidecar = comfy_base / "models" / "upscale_models" / "4x-UltraSharp.pth.civitai.json"
    assert sidecar.exists()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["relative_dir"] == "upscale_models"


def test_root_can_upload_comfyui_model_file_into_local_models_dir(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    comfy_base.mkdir()
    (comfy_base / "main.py").write_text("# comfy", encoding="utf-8")
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
        },
    ).test_client()

    uploaded = client.post(
        "/api/root/comfyui/model-upload",
        data={
            "type": "lora",
            "base_dir": str(comfy_base),
            "model_file": (io.BytesIO(b"fake-model-bytes"), "manual_test_lora.safetensors", "application/octet-stream"),
        },
        content_type="multipart/form-data",
    )

    assert uploaded.status_code == 200
    body = uploaded.get_json()
    assert body["upload"]["filename"] == "manual_test_lora.safetensors"
    assert body["upload"]["source"] == "manual_upload"
    saved = comfy_base / "models" / "loras" / "manual_test_lora.safetensors"
    assert saved.exists()
    assert saved.read_bytes() == b"fake-model-bytes"
    sidecar = comfy_base / "models" / "loras" / "manual_test_lora.safetensors.civitai.json"
    sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert sidecar_data["source"] == "manual_upload"
    assert sidecar_data["uploaded_by"] == "root"


def test_root_can_upload_comfyui_model_file_when_generation_backend_is_remote(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    comfy_base.mkdir()
    (comfy_base / "main.py").write_text("# comfy", encoding="utf-8")
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={
            "comfyui_connection_mode": "remote",
            "comfyui_remote_api_url": "http://example.invalid:8192",
            "comfyui_base_dir": str(comfy_base),
        },
    ).test_client()

    uploaded = client.post(
        "/api/root/comfyui/model-upload",
        data={
            "type": "lora",
            "base_dir": str(comfy_base),
            "model_file": (io.BytesIO(b"remote-mode-bytes"), "remote_mode_lora.safetensors", "application/octet-stream"),
        },
        content_type="multipart/form-data",
    )

    assert uploaded.status_code == 200
    saved = comfy_base / "models" / "loras" / "remote_mode_lora.safetensors"
    assert saved.exists()
    assert saved.read_bytes() == b"remote-mode-bytes"


def test_root_can_upload_comfyui_model_file_into_custom_relative_dir(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    comfy_base.mkdir()
    (comfy_base / "main.py").write_text("# comfy", encoding="utf-8")
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
        },
    ).test_client()

    uploaded = client.post(
        "/api/root/comfyui/model-upload",
        data={
            "type": "upscale",
            "base_dir": str(comfy_base),
            "relative_dir": "upscale_models/custom",
            "model_file": (io.BytesIO(b"fake-upscale-bytes"), "custom_upscale.pth", "application/octet-stream"),
        },
        content_type="multipart/form-data",
    )

    assert uploaded.status_code == 200
    body = uploaded.get_json()
    assert body["upload"]["relative_dir"] == "upscale_models/custom"
    saved = comfy_base / "models" / "upscale_models" / "custom" / "custom_upscale.pth"
    assert saved.exists()
    sidecar = comfy_base / "models" / "upscale_models" / "custom" / "custom_upscale.pth.civitai.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["relative_dir"] == "upscale_models/custom"


def test_comfyui_model_upload_rejects_invalid_extension(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    comfy_base.mkdir()
    (comfy_base / "main.py").write_text("# comfy", encoding="utf-8")
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
        },
    ).test_client()

    uploaded = client.post(
        "/api/root/comfyui/model-upload",
        data={
            "type": "checkpoint",
            "base_dir": str(comfy_base),
            "model_file": (io.BytesIO(b"bad-model"), "manual_test_lora.txt", "text/plain"),
        },
        content_type="multipart/form-data",
    )

    assert uploaded.status_code == 400
    assert "模型副檔名必須是" in uploaded.get_json()["msg"]


def test_comfyui_model_upload_rejects_relative_dir_traversal(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    comfy_base.mkdir()
    (comfy_base / "main.py").write_text("# comfy", encoding="utf-8")
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={
            "comfyui_connection_mode": "local",
            "comfyui_base_dir": str(comfy_base),
        },
    ).test_client()

    uploaded = client.post(
        "/api/root/comfyui/model-upload",
        data={
            "type": "upscale",
            "base_dir": str(comfy_base),
            "relative_dir": "../escape",
            "model_file": (io.BytesIO(b"fake-upscale-bytes"), "custom_upscale.pth", "application/octet-stream"),
        },
        content_type="multipart/form-data",
    )

    assert uploaded.status_code == 400
    assert "相對路徑" in uploaded.get_json()["msg"]


def test_comfyui_civitai_async_download_job_reports_progress_and_result(tmp_path, monkeypatch):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "comfyui_portable"
    comfy_base.mkdir()
    (comfy_base / "main.py").write_text("# comfy", encoding="utf-8")
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(
        db_path,
        storage_root,
        actor=lambda: root_actor,
        settings={
            "comfyui_civitai_api_key": "secret-token",
            "comfyui_base_dir": str(comfy_base),
        },
    ).test_client()

    model_bytes = b"async-model-bytes"

    def fake_urlopen(request_obj, timeout=0):
        url = request_obj.full_url
        if url == "https://civitai.com/api/v1/models/123456":
            payload = {
                "id": 123456,
                "name": "Fancy Model",
                "type": "LORA",
                "creator": {"username": "artist"},
                "modelVersions": [
                    {
                        "id": 2002,
                        "name": "v2",
                        "baseModel": "SDXL",
                        "trainedWords": ["fancy style", "cinematic"],
                        "downloadUrl": "https://civitai.com/api/download/models/2002",
                        "files": [
                            {
                                "id": 3002,
                                "name": "fancy_v2.safetensors",
                                "sizeKB": 4096,
                                "downloadUrl": "https://civitai.com/api/download/models/2002",
                                "type": "Model",
                            }
                        ],
                    },
                ],
            }
            return _FakeResponse(url=url, body=json.dumps(payload), headers={"Content-Type": "application/json; charset=utf-8"})
        if url.startswith("https://civitai.com/api/download/models/2002"):
            return _FakeResponse(
                url=url,
                body=model_bytes,
                headers={
                    "Content-Disposition": 'attachment; filename="fancy_v2.safetensors"',
                    "Content-Length": str(len(model_bytes)),
                },
            )
        raise AssertionError(f"unexpected urlopen target: {url}")

    monkeypatch.setattr(comfyui_routes.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        comfyui_routes.socket,
        "getaddrinfo",
        lambda host, port, type=0: [(None, None, None, None, ("104.21.72.187", port or 443))],
    )

    started = client.post(
        "/api/root/comfyui/civitai/download",
        json={
            "page_url": "https://civitai.com/models/123456/fancy-model?modelVersionId=2002",
            "version_id": 2002,
            "file_id": 3002,
            "type": "lora",
            "base_dir": str(comfy_base),
            "async_progress": True,
        },
    )

    assert started.status_code == 200
    start_body = started.get_json()
    assert start_body["ok"] is True
    assert start_body["async"] is True
    job_id = start_body["job"]["job_id"]

    final_body = None
    for _ in range(40):
        polled = client.get(f"/api/root/comfyui/download-jobs/{job_id}")
        assert polled.status_code == 200
        final_body = polled.get_json()
        assert final_body["ok"] is True
        if final_body["job"]["status"] == "completed":
            break
        time.sleep(0.05)

    assert final_body is not None
    assert final_body["job"]["status"] == "completed"
    assert final_body["job"]["progress"]["percent"] == 100
    assert final_body["job"]["progress"]["bytes_written"] == len(model_bytes)
    assert final_body["job"]["result"]["filename"] == "fancy_v2.safetensors"
    assert final_body["job"]["result"]["civitai"]["trained_words"] == ["fancy style", "cinematic"]
    assert (comfy_base / "models" / "loras" / "fancy_v2.safetensors").read_bytes() == model_bytes

def test_comfyui_save_stores_generated_image_in_user_storage(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()
    preview = _generate_preview(client)

    saved = client.post(
        "/api/comfyui/save",
        json={
            "image_ref": preview["image_ref"],
            "virtual_path": "/ComfyUI/smoke.png",
        },
    )

    assert saved.status_code == 200
    body = saved.get_json()
    assert body["storage_file"]["virtual_path"] == "/ComfyUI/smoke.png"
    assert body["file"]["file_id"]

    stored_path = storage_root / "users" / "1" / body["file"]["file_id"]
    assert not stored_path.exists()
    assert list(storage_root.glob("users/1/*/hackme_web_00001_.png"))


def test_comfyui_image_ref_is_bound_to_generating_user(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    alice_client = _build_app(db_path, storage_root).test_client()
    preview = _generate_preview(alice_client)
    bob_actor = {
        "id": 2,
        "username": "bob",
        "role": "user",
        "member_level": "trusted",
        "effective_level": "trusted",
        "sanction_status": "none",
    }
    bob_client = _build_app(db_path, storage_root, actor=lambda: bob_actor).test_client()

    stolen = bob_client.post(
        "/api/comfyui/save",
        json={"image_ref": preview["image_ref"], "virtual_path": "/ComfyUI/stolen.png"},
    )

    assert stolen.status_code == 404
    assert "找不到" in stolen.get_json()["msg"]


def test_comfyui_save_defaults_to_output_folder_and_album(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()
    preview = _generate_preview(client)

    saved = client.post(
        "/api/comfyui/save",
        json={
            "image_ref": preview["image_ref"],
        },
    )

    assert saved.status_code == 200
    body = saved.get_json()
    assert body["storage_file"]["virtual_path"] == "/output/hackme_web_00001_.png"
    assert body["album"]["title"] == "output"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    folder = conn.execute(
        "SELECT * FROM storage_folders WHERE owner_user_id=1 AND virtual_path='/output' AND deleted_at IS NULL"
    ).fetchone()
    album_file = conn.execute(
        "SELECT * FROM album_files WHERE album_id=? AND file_id=? AND deleted_at IS NULL",
        (body["album"]["id"], body["file"]["file_id"]),
    ).fetchone()
    conn.close()
    assert folder is not None
    assert album_file["storage_file_id"] == body["storage_file"]["id"]


def test_output_album_syncs_files_moved_into_output_folder(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor = _actor()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        create_storage_folder(conn, actor=actor, path="/output")
        upload = create_uploaded_file_record(
            conn,
            owner_user_id=actor["id"],
            storage_path="users/1/manual/external.png",
            privacy_mode="standard_plain",
            size_bytes=12,
            original_filename="external.png",
            mime_type="image/png",
            user=actor,
        )
        file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (upload["file_id"],)).fetchone()
        storage_file, msg = create_storage_file_entry(
            conn,
            actor=actor,
            file_row=file_row,
            virtual_path="/imports/external.png",
            display_name="external.png",
            source="test",
        )
        assert msg is None

        moved, msg = move_storage_file(conn, actor=actor, storage_file_id=storage_file["id"], new_virtual_path="/output")
        assert msg is None
        album, msg = ensure_output_album(conn, actor=actor)

        assert msg is None
        assert moved["virtual_path"] == "/output/external.png"
        assert album["title"] == "output"
        assert [file["virtual_path"] for file in album["files"]] == ["/output/external.png"]
    finally:
        conn.close()


def test_output_album_repairs_file_record_stored_at_output_path(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    actor = _actor()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        upload = create_uploaded_file_record(
            conn,
            owner_user_id=actor["id"],
            storage_path="users/1/manual/external.png",
            privacy_mode="standard_plain",
            size_bytes=12,
            original_filename="external.png",
            mime_type="image/png",
            user=actor,
        )
        file_row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (upload["file_id"],)).fetchone()
        storage_file, msg = create_storage_file_entry(
            conn,
            actor=actor,
            file_row=file_row,
            virtual_path="/output",
            display_name="output",
            source="test",
        )
        assert msg is None

        album, msg = ensure_output_album(conn, actor=actor)
        repaired = conn.execute("SELECT * FROM storage_files WHERE id=?", (storage_file["id"],)).fetchone()
        folder = conn.execute(
            "SELECT * FROM storage_folders WHERE owner_user_id=1 AND virtual_path='/output' AND deleted_at IS NULL"
        ).fetchone()

        assert msg is None
        assert folder is not None
        assert repaired["virtual_path"] == "/output/external.png"
        assert [file["virtual_path"] for file in album["files"]] == ["/output/external.png"]
    finally:
        conn.close()


def test_output_album_prunes_files_moved_out_of_output_folder(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()
    preview = _generate_preview(client)

    saved = client.post(
        "/api/comfyui/save",
        json={
            "image_ref": preview["image_ref"],
        },
    )
    assert saved.status_code == 200
    body = saved.get_json()

    actor = _actor()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        moved, msg = move_storage_file(
            conn,
            actor=actor,
            storage_file_id=body["storage_file"]["id"],
            new_virtual_path="/imports/hackme_web_00001_.png",
        )
        assert msg is None
        album, msg = ensure_output_album(conn, actor=actor)

        assert moved["virtual_path"] == "/imports/hackme_web_00001_.png"
        assert msg is None
        assert album["files"] == []
        assert album["removed_count"] == 1
    finally:
        conn.close()


def test_comfyui_discard_deletes_original_comfyui_file_in_local_mode(tmp_path):
    FakeComfyUIClient.discarded = []
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    comfy_base = tmp_path / "ComfyUI"
    storage_root.mkdir()
    (comfy_base / "output").mkdir(parents=True)
    _init_db(db_path)
    client = _build_app(
        db_path,
        storage_root,
        settings={"comfyui_connection_mode": "local", "comfyui_base_dir": str(comfy_base)},
    ).test_client()
    preview = _generate_preview(client)

    discarded = client.post(
        "/api/comfyui/discard",
        json={
            "image_ref": preview["image_ref"],
            "prompt_id": preview["prompt_id"],
        },
    )

    assert discarded.status_code == 200
    body = discarded.get_json()
    assert body["discard"]["file_deleted"] is True
    assert body["discard"]["history_deleted"] is True
    assert FakeComfyUIClient.discarded == [{
        "image_ref": {"filename": "hackme_web_00001_.png", "subfolder": "", "type": "output"},
        "prompt_id": "prompt-1",
        "local_base_dir": str(comfy_base),
        "allow_api_delete": False,
    }]


def test_comfyui_discard_remote_mode_clears_preview_without_deleting_source(tmp_path):
    FakeComfyUIClient.discarded = []
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root, settings={"comfyui_connection_mode": "remote"}).test_client()
    preview = _generate_preview(client)

    discarded = client.post(
        "/api/comfyui/discard",
        json={
            "image_ref": preview["image_ref"],
            "prompt_id": preview["prompt_id"],
        },
    )

    assert discarded.status_code == 200
    body = discarded.get_json()
    assert body["ok"] is True
    assert body["warning"] == "source_file_not_deleted"
    assert body["discard"]["file_delete_supported"] is False
    assert FakeComfyUIClient.discarded == []


def test_comfyui_discard_tolerates_plain_text_history_response(tmp_path, monkeypatch):
    output_dir = tmp_path / "comfy-output"
    output_dir.mkdir()
    image_path = output_dir / "plain-history.png"
    image_path.write_bytes(b"fake-png")
    monkeypatch.setenv("COMFYUI_OUTPUT_DIR", str(output_dir))
    calls = []

    class PlainTextResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"OK"

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return PlainTextResponse()

    monkeypatch.setattr("services.comfyui.client.urllib.request.urlopen", fake_urlopen)

    result = ComfyUIClient("http://fake-comfyui").discard_image(
        {"filename": "plain-history.png", "subfolder": "", "type": "output"},
        prompt_id="prompt-plain",
    )

    assert result["file_deleted"] is True
    assert result["history_deleted"] is True
    assert not image_path.exists()
    assert calls == ["http://fake-comfyui/history"]


def test_comfyui_discard_without_file_delete_endpoint_clears_preview_with_warning(tmp_path):
    class UnsupportedDeleteClient(FakeComfyUIClient):
        def discard_image(self, image_ref, *, prompt_id=None, **kwargs):
            return {
                "file_deleted": False,
                "file_missing": False,
                "file_delete_supported": False,
                "history_deleted": bool(prompt_id),
            }

    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    comfy_base = tmp_path / "ComfyUI"
    (comfy_base / "output").mkdir(parents=True)
    client = _build_app(
        db_path,
        storage_root,
        settings={"comfyui_connection_mode": "local", "comfyui_base_dir": str(comfy_base)},
        comfyui_client=UnsupportedDeleteClient(),
    ).test_client()
    preview = _generate_preview(client)

    discarded = client.post(
        "/api/comfyui/discard",
        json={
            "image_ref": preview["image_ref"],
            "prompt_id": preview["prompt_id"],
        },
    )

    assert discarded.status_code == 200
    body = discarded.get_json()
    assert body["ok"] is True
    assert body["warning"] == "source_file_not_deleted"
    assert body["discard"]["history_deleted"] is True
    assert "原始檔可能仍留在 ComfyUI output" in body["msg"]


def test_comfyui_interrupt_requests_backend_interrupt(tmp_path):
    FakeComfyUIClient.interrupted = 0
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    root_actor = {"id": 1, "username": "root", "role": "super_admin"}
    client = _build_app(db_path, storage_root, actor=lambda: root_actor).test_client()

    interrupted = client.post("/api/comfyui/interrupt", json={})

    assert interrupted.status_code == 200
    body = interrupted.get_json()
    assert body["ok"] is True
    assert body["interrupt"]["interrupted"] is True
    assert FakeComfyUIClient.interrupted == 1


def test_comfyui_interrupt_without_owned_generation_does_not_interrupt_backend(tmp_path):
    FakeComfyUIClient.interrupted = 0
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    client = _build_app(db_path, storage_root).test_client()

    interrupted = client.post("/api/comfyui/interrupt", json={})

    assert interrupted.status_code == 200
    body = interrupted.get_json()
    assert body["ok"] is True
    assert body["interrupt"]["backend_interrupted"] is False
    assert body["interrupt"]["reason"] == "no_owned_generation"
    assert FakeComfyUIClient.interrupted == 0


def test_comfyui_interrupt_denies_shared_backend_when_other_user_is_generating(tmp_path):
    FakeComfyUIClient.interrupted = 0
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    shared_backend_url = "http://localhost:8192"
    active_generations = {
        "own": {"user_id": 1, "username": "test", "backend_url": shared_backend_url},
        "other": {"user_id": 2, "username": "admin", "backend_url": shared_backend_url},
    }
    client = _build_app(
        db_path,
        storage_root,
        extra_deps={"comfyui_active_generations": active_generations},
    ).test_client()

    interrupted = client.post("/api/comfyui/interrupt", json={})

    assert interrupted.status_code == 200
    body = interrupted.get_json()
    assert body["ok"] is True
    assert body["interrupt"]["backend_interrupted"] is False
    assert body["interrupt"]["reason"] == "shared_backend_busy"
    assert FakeComfyUIClient.interrupted == 0


def test_comfyui_interrupt_allows_owned_generation_when_backend_is_not_shared(tmp_path):
    FakeComfyUIClient.interrupted = 0
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    active_generations = {"own": {"user_id": 1, "username": "test", "backend_url": "http://localhost:8192"}}
    client = _build_app(
        db_path,
        storage_root,
        extra_deps={"comfyui_active_generations": active_generations},
    ).test_client()

    interrupted = client.post("/api/comfyui/interrupt", json={})

    assert interrupted.status_code == 200
    body = interrupted.get_json()
    assert body["ok"] is True
    assert body["interrupt"]["backend_interrupted"] is True
    assert body["interrupt"]["reason"] == "owned_generation_only"
    assert FakeComfyUIClient.interrupted == 1


def test_comfyui_interrupt_tolerates_plain_text_response(monkeypatch):
    calls = []

    class PlainTextResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"interrupted"

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return PlainTextResponse()

    monkeypatch.setattr("services.comfyui.client.urllib.request.urlopen", fake_urlopen)

    result = ComfyUIClient("http://fake-comfyui").interrupt()

    assert result == {"raw": "interrupted"}
    assert calls == ["http://fake-comfyui/interrupt"]


def test_comfyui_health_check_timeout_is_reported_as_comfyui_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr("services.comfyui.client.urllib.request.urlopen", fake_urlopen)

    with pytest.raises(ComfyUIError, match="ComfyUI 連線逾時"):
        ComfyUIClient("http://fake-comfyui").health_check(timeout=1)


def test_comfyui_wait_extends_timeout_while_prompt_is_queued(monkeypatch):
    clock = {"now": 0.0}
    progress_events = []

    class QueuedThenDoneClient:
        timeout = 1

        def _json_request(self, path, *, timeout=None):
            assert path == "/history/queued-prompt"
            if clock["now"] < 12:
                return {}
            return {
                "queued-prompt": {
                    "status": {"completed": True, "status_str": "success"},
                    "outputs": {"9": {"images": [{"filename": "done.png", "subfolder": "", "type": "output"}]}},
                }
            }

    def fake_time():
        return clock["now"]

    def fake_sleep(seconds):
        clock["now"] += max(float(seconds), 0.15)

    monkeypatch.setattr(comfy_execution.time, "time", fake_time)
    monkeypatch.setattr(comfy_execution.time, "sleep", fake_sleep)
    monkeypatch.setattr(comfy_execution, "QUEUE_TIMEOUT_EXTENSION_SECONDS", 5)
    monkeypatch.setattr(comfy_execution, "QUEUE_MAX_TIMEOUT_SECONDS", 20)

    images = comfy_execution.wait_for_images(
        QueuedThenDoneClient(),
        "queued-prompt",
        timeout_seconds=3,
        poll_interval=1,
        expected_count=1,
        error_cls=ComfyUIError,
        progress_callback=progress_events.append,
    )

    assert images == [{"filename": "done.png", "subfolder": "", "type": "output"}]
    assert any(event.get("timeout_extended") is True for event in progress_events)
    assert any(int(event.get("timeout_seconds") or 0) > 3 for event in progress_events)


def test_comfyui_save_can_add_generated_image_to_album(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    album, msg = create_album(conn, actor=_actor(), title="AI Gallery")
    assert msg is None
    conn.commit()
    conn.close()
    client = _build_app(db_path, storage_root).test_client()
    preview = _generate_preview(client)

    saved = client.post(
        "/api/comfyui/save",
        json={
            "image_ref": preview["image_ref"],
            "virtual_path": "/ComfyUI/album.png",
            "album_id": album["id"],
        },
    )

    assert saved.status_code == 200
    body = saved.get_json()
    assert body["album"]["id"] == album["id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT file_id, storage_file_id FROM album_files WHERE album_id=? AND deleted_at IS NULL",
        (album["id"],),
    ).fetchone()
    conn.close()
    assert row["file_id"] == body["file"]["file_id"]
    assert row["storage_file_id"] == body["storage_file"]["id"]


def test_comfyui_share_creates_comfyui_thread_with_preview_grant(tmp_path):
    db_path = tmp_path / "comfyui.db"
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    _init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    album, msg = create_album(conn, actor=_actor(), title="Shared AI")
    assert msg is None
    conn.commit()
    conn.close()
    client = _build_app(db_path, storage_root).test_client()
    preview = _generate_preview(client)

    shared = client.post(
        "/api/comfyui/share",
        json={
            "image_ref": preview["image_ref"],
            "virtual_path": "/ComfyUI/share.png",
            "album_id": album["id"],
            "title": "My ComfyUI share",
            "note": "這張圖的心得",
            "generation": {
                "model": "dream.safetensors",
                "prompt": "a quiet test image",
                "negative_prompt": "noise",
                "width": 512,
                "height": 768,
                "steps": 18,
                "cfg": 6.5,
                "seed": 123,
                "batch_size": 2,
                "sampler_name": "euler",
                "scheduler": "normal",
            },
        },
    )

    assert shared.status_code == 200
    body = shared.get_json()
    assert body["thread"]["title"] == "My ComfyUI share"
    file_id = body["file"]["file_id"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    thread = conn.execute("SELECT * FROM forum_threads WHERE id=?", (body["thread"]["id"],)).fetchone()
    grant = conn.execute(
        "SELECT * FROM file_access_grants WHERE file_id=? AND context_type='forum_thread' AND context_id=?",
        (file_id, str(body["thread"]["id"])),
    ).fetchone()
    album_file = conn.execute(
        "SELECT id FROM album_files WHERE album_id=? AND file_id=? AND deleted_at IS NULL",
        (album["id"], file_id),
    ).fetchone()
    conn.close()
    assert thread["board_id"] == body["thread"]["board_id"]
    assert "[[comfyui-image:" + file_id + "]]" in thread["content"]
    assert "這張圖的心得" in thread["content"]
    assert "a quiet test image" in thread["content"]
    assert "張數：2" in thread["content"]
    assert grant["granted_to_role"] == "user"
    assert grant["can_preview"] == 1
    assert album_file is not None


def test_comfyui_frontend_is_wired():
    index_html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    core_js = (ROOT / "public" / "js" / "00-core.js").read_text(encoding="utf-8")
    admin_js = (
        (ROOT / "public" / "js" / "50-admin.js").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "public" / "js" / "51-admin-server-mode-launch-check.js").read_text(encoding="utf-8")
    )
    bootstrap_js = (ROOT / "public" / "js" / "90-bootstrap.js").read_text(encoding="utf-8")
    community_js = (ROOT / "public" / "js" / "25-community.js").read_text(encoding="utf-8")
    comfyui_js = ((ROOT / "public" / "js" / "36-comfyui.js").read_text(encoding="utf-8") + "\n" + (ROOT / "public" / "js" / "36-comfyui-workflows.js").read_text(encoding="utf-8"))
    css = (ROOT / "public" / "styles.css").read_text(encoding="utf-8")
    platform_settings_py = (ROOT / "services" / "platform" / "settings.py").read_text(encoding="utf-8")
    comfyui_settings_py = (ROOT / "services" / "comfyui" / "settings.py").read_text(encoding="utf-8")
    smoke = (ROOT / "scripts" / "security" / "pentest" / "run_functional_smoke.sh").read_text(encoding="utf-8")

    assert 'id="tab-module-comfyui"' in index_html
    assert 'id="module-comfyui"' in index_html
    assert 'id="comfyui-model-select"' in index_html
    assert 'id="comfyui-vae-select"' in index_html
    assert 'id="comfyui-generation-mode"' in index_html
    assert 'id="comfyui-denoise-strength"' in index_html
    assert 'id="comfyui-upscale-model"' in index_html
    assert 'id="comfyui-source-image-file"' in index_html
    assert 'id="comfyui-mask-image-file"' in index_html
    assert 'id="comfyui-control-image-file"' in index_html
    assert 'id="comfyui-controlnet-enabled"' in index_html
    assert 'id="comfyui-controlnet-type"' in index_html
    assert 'id="comfyui-controlnet-model"' in index_html
    assert 'id="comfyui-controlnet-preprocessor"' in index_html
    assert 'id="comfyui-control-strength"' in index_html
    assert 'id="comfyui-control-start"' in index_html
    assert 'id="comfyui-control-end"' in index_html
    assert 'id="comfyui-outpaint-left"' in index_html
    assert 'id="comfyui-outpaint-top"' in index_html
    assert 'id="comfyui-outpaint-right"' in index_html
    assert 'id="comfyui-outpaint-bottom"' in index_html
    assert 'id="comfyui-outpaint-feathering"' in index_html
    assert 'id="comfyui-history-refresh-btn"' in index_html
    assert 'id="comfyui-history-list"' in index_html
    assert 'id="comfyui-lora-select"' in index_html
    assert 'id="comfyui-lora-add-btn"' in index_html
    assert 'id="comfyui-lora-count"' in index_html
    assert 'id="comfyui-selected-loras"' in index_html
    assert 'id="comfyui-embedding-shortcuts"' in index_html
    assert 'id="comfyui-generate-btn"' in index_html
    assert 'id="comfyui-interrupt-btn"' in index_html
    assert 'id="comfyui-load-draft-btn"' in index_html
    assert 'id="comfyui-batch-size"' in index_html
    assert 'id="comfyui-run-count"' in index_html
    assert 'id="comfyui-save-btn"' in index_html
    assert 'id="comfyui-album-select"' in index_html
    assert 'id="comfyui-share-btn"' in index_html
    assert 'id="comfyui-progress-panel"' in index_html
    assert 'id="comfyui-model-download-btn"' in index_html
    assert 'id="comfyui-model-source-mode"' in index_html
    assert 'id="comfyui-model-source-civitai"' in index_html
    assert 'id="comfyui-model-source-upload"' in index_html
    assert 'id="comfyui-model-upload-file"' in index_html
    assert 'id="comfyui-model-upload-btn"' in index_html
    assert 'id="comfyui-model-relative-path"' in index_html
    assert '<option value="upscale">放大模型 / Upscaler</option>' in index_html
    assert 'id="comfyui-model-download-progress"' in index_html
    assert 'id="comfyui-model-download-progress-label"' in index_html
    assert 'id="comfyui-model-download-progress-percent"' in index_html
    assert 'id="comfyui-model-download-progress-bar"' in index_html
    assert 'id="comfyui-model-download-progress-detail"' in index_html
    assert 'id="comfyui-civitai-inspect-btn"' in index_html
    assert 'id="comfyui-civitai-search-btn"' in index_html
    assert 'id="comfyui-civitai-search-query"' in index_html
    assert 'id="comfyui-civitai-search-base-model"' in index_html
    assert 'id="comfyui-civitai-search-type"' in index_html
    assert 'id="comfyui-civitai-search-nsfw"' in index_html
    assert 'id="comfyui-civitai-search-status"' in index_html
    assert 'id="comfyui-civitai-search-results"' in index_html
    assert 'id="comfyui-civitai-url"' in index_html
    assert 'id="comfyui-civitai-version"' in index_html
    assert 'id="comfyui-civitai-file"' in index_html
    assert 'id="comfyui-civitai-trained-words"' in index_html
    assert 'id="comfyui-start-btn"' in index_html
    assert 'id="comfyui-stop-btn"' in index_html
    assert 'id="comfyui-mode-badge"' in index_html
    assert 'id="comfyui-mode-note"' in index_html
    assert 'id="comfyui-mode-detail"' in index_html
    assert '目前模式：讀取中' in index_html
    assert '模式讀取中' in index_html
    assert 'id="comfyui-root-model-details"' in index_html
    assert 'id="comfyui-root-model-mode-hint"' in index_html
    assert 'root 模型匯入（Civitai / 檔案上傳）' in index_html
    assert 'data-comfyui-view="models">Civitai / 模型管理</button>' in index_html
    assert 'data-comfyui-view="models" hidden' not in index_html
    assert 'id="comfyui-open-civitai-panel-btn"' not in index_html
    assert 'id="comfyui-root-model-access-note"' in index_html
    assert '和上方生圖表單分開' in index_html
    assert 'Civitai 模型匯入區會在本地與遠端模式常駐可見' in index_html
    assert '<option value="embedding">Embedding / TI</option>' in index_html
    assert '<option value="vae">VAE</option>' in index_html
    assert '<option value="controlnet">ControlNet</option>' in index_html
    assert '<option value="hypernetwork">Hypernetwork</option>' not in index_html
    assert 'id="s-comfyui-connection-mode"' in index_html
    assert '<option value="diffusers">diffusers</option>' in index_html
    assert 'data-comfyui-view="hf">HF / Diffusers</button>' in index_html
    assert 'id="s-comfyui-remote-api-url"' in index_html
    assert '兩組設定彼此獨立' in index_html
    assert 'ComfyUI 決定本地或遠端執行後端' in index_html
    assert 'id="comfyui-diffusers-settings"' in index_html
    assert 'data-comfyui-settings-family-panel="hf"' in index_html
    assert 'id="s-comfyui-diffusers-model-repo"' in index_html
    assert 'id="s-comfyui-huggingface-api-token"' in index_html
    assert 'id="s-comfyui-huggingface-cache-root"' in index_html
    assert 'id="s-comfyui-diffusers-device"' in index_html
    assert 'id="s-comfyui-diffusers-cuda-fallback-to-cpu"' in index_html
    assert 'id="s-comfyui-diffusers-dtype"' in index_html
    assert 'id="s-comfyui-base-dir"' in index_html
    assert 'id="s-comfyui-local-start-script"' in index_html
    assert 'id="comfyui-local-start-template-link"' in index_html
    assert 'href="/api/root/comfyui/local-start-template"' in index_html
    assert 'id="comfyui-civitai-settings"' in index_html
    assert 'id="comfyui-civitai-settings" data-comfyui-settings-family-panel="comfyui"' in index_html
    assert 'id="s-comfyui-civitai-api-key"' in index_html
    assert "function canManageComfyuiLocalModels" in comfyui_js
    assert 'return currentUser === "root";' in comfyui_js
    assert 'if (panel) panel.style.display = showLocalModels ? "" : "none";' in comfyui_js
    assert "if (modelsTab) modelsTab.hidden = false;" in comfyui_js
    assert 'if (accessNote) accessNote.style.display = showLocalModels ? "none" : "";' in comfyui_js
    assert '目前是雲端 / 遠端模式；Civitai 匯入區仍可使用' in comfyui_js
    assert "/js/36-comfyui.js?v=20260607-image-favorites-history-favorite-dashboard" in index_html
    assert "/styles.css?v=20260607-image-favorites-history-favorite" in index_html
    assert "width: min(420px, 100%);" in css
    assert "max-height: 320px;" in css
    assert ".comfyui-root-details" in css
    assert 'id="s-comfyui-api-port"' in index_html
    assert 'id="comfyui-test-connection-btn"' in index_html
    assert 'id="comfyui-test-connection-status"' in index_html
    assert 'id="s-comfyui-max-batch-size"' in index_html
    assert 'tabModuleComfyui.style.display = canAccessModule("comfyui") ? "" : "none"' in core_js
    assert 'switchModuleTab("comfyui")' in bootstrap_js
    assert 'normTab === "comfyui"' in admin_js
    assert '本地模式會測試本地 API；若產圖時 API 未啟動，後端會嘗試執行啟動腳本。' in admin_js
    assert 'ComfyUI / GGUF 遠端模式只負責呼叫指定 API 生圖；Civitai API Key 與模型匯入區會常駐可見' in admin_js
    assert 'if (civitaiBox) civitaiBox.style.display = settingsFamily === "comfyui" ? "" : "none";' in admin_js
    assert 'if (civitaiInput) civitaiInput.disabled = settingsFamily !== "comfyui";' in admin_js
    assert 'HF 頁籤會檢查 Hugging Face repo 與 Python / Diffusers 套件' in admin_js
    assert "function openComfyuiCivitaiPanel()" in comfyui_js
    assert "comfyui-open-civitai-panel-btn" not in comfyui_js
    assert "updateComfyuiRootPanelVisibility();" in comfyui_js
    assert 'apiFetch(API + "/comfyui/generate"' in comfyui_js
    assert 'apiFetch(API + "/comfyui/billing-quote"' in comfyui_js
    assert 'apiFetch(API + "/root/comfyui/civitai/search"' in comfyui_js
    assert 'apiFetch(API + "/root/comfyui/civitai/inspect"' in comfyui_js
    assert 'apiFetch(API + "/root/comfyui/civitai/download"' in comfyui_js
    assert 'apiFetch(API + "/root/comfyui/model-upload"' in comfyui_js
    assert 'apiFetch(API + requestPath' in comfyui_js
    assert 'apiFetch(API + "/comfyui/status" + comfyuiRequestQuery()' in comfyui_js
    assert 'setComfyuiIdleSuspend("comfyui_generate", !!busy, `${isComfyuiDiffusersMode() ? "Diffusers" : "ComfyUI"} 產圖中`);' in comfyui_js
    assert 'setComfyuiIdleSuspend("comfyui_start_local", true, "ComfyUI 啟動中");' in comfyui_js
    assert 'setComfyuiIdleSuspend("comfyui_model_download", true, "ComfyUI 模型下載中");' in comfyui_js
    assert 'apiFetch(API + "/comfyui/start"' in comfyui_js
    assert 'apiFetch(API + "/root/comfyui/stop"' in comfyui_js
    assert 'apiFetch(API + `/comfyui/jobs/${encodeURIComponent(jobId)}`' in comfyui_js
    assert 'apiFetch(API + `/root/comfyui/download-jobs/${encodeURIComponent(jobId)}`' in comfyui_js
    assert "function startLocalComfyui()" in comfyui_js
    assert "function stopLocalComfyui()" in comfyui_js
    assert "function comfyuiConnectionModeLabel(mode = comfyuiConnectionMode)" in comfyui_js
    assert "function comfyuiConnectionModeDetail(mode = comfyuiConnectionMode)" in comfyui_js
    assert "function fillComfyuiGenerationModes(values = [])" in comfyui_js
    assert "function fillComfyuiControlnetTypes(types = {})" in comfyui_js
    assert "function fillComfyuiUpscaleModels(values = [])" in comfyui_js
    assert "function updateComfyuiModelSourceMode()" in comfyui_js
    assert "function comfyuiDefaultModelRelativeDir(type = comfyuiSelectedModelDownloadType())" in comfyui_js
    assert "function updateComfyuiModelRelativePathHint()" in comfyui_js
    assert "function uploadComfyuiModelFile()" in comfyui_js
    assert "function updateComfyuiModeVisibility()" in comfyui_js
    assert "function comfyuiBuildGenerateRequest(payload)" in comfyui_js
    assert "function loadComfyuiHistory()" in comfyui_js
    assert "function applyComfyuiHistoryToForm(historyId)" in comfyui_js
    assert "function rerunComfyuiHistory(historyId)" in comfyui_js
    assert "ComfyUI Workflow Layout Builder" in index_html
    assert 'id="comfyui-workflow-title"' in index_html
    assert 'id="comfyui-workflow-purpose"' in index_html
    assert 'id="comfyui-workflow-comfyui-version"' in index_html
    assert 'id="comfyui-workflow-project-version"' in index_html
    assert 'id="comfyui-workflow-schema-version"' in index_html
    assert 'id="comfyui-workflow-is-default"' in index_html
    assert 'id="comfyui-workflow-new-btn"' in index_html
    assert 'id="comfyui-workflow-starter-txt2img-btn"' in index_html
    assert 'id="comfyui-workflow-node-template"' in index_html
    assert 'id="comfyui-workflow-add-node-btn"' in index_html
    assert 'id="comfyui-workflow-open-visual-btn"' in index_html
    assert "開啟節點連線編輯器" in index_html
    assert 'id="comfyui-workflow-json"' in index_html
    assert 'id="comfyui-workflow-layout-json"' in index_html
    assert 'id="comfyui-workflow-my-list"' in index_html
    assert 'id="comfyui-workflow-official-list"' in index_html
    assert 'id="comfyui-workflow-shared-list"' in index_html
    assert "function loadComfyuiWorkflowPresets()" in comfyui_js
    assert "function exportCurrentComfyuiWorkflow()" in comfyui_js
    assert "function importComfyuiWorkflowPreset()" in comfyui_js
    assert "function updateComfyuiWorkflowPreset()" in comfyui_js
    assert "function runComfyuiWorkflowPreset(presetId)" in comfyui_js
    assert "function duplicateComfyuiWorkflowPreset(presetId)" in comfyui_js
    assert "function setDefaultComfyuiWorkflowPreset(presetId)" in comfyui_js
    assert "function publishComfyuiWorkflowPresetOfficial(presetId)" in comfyui_js
    assert "LoadImageMask" in comfyui_js
    assert "VAEEncodeForInpaint" in comfyui_js
    assert "ImagePadForOutpaint" in comfyui_js
    assert "ControlNetApplyAdvanced" in comfyui_js
    assert "ImageUpscaleWithModel" in comfyui_js
    assert "function prepareComfyuiVisualWorkflowEditorInput()" in comfyui_js
    assert "hackme_comfyui_workflow_editor_input" in comfyui_js
    assert 'apiFetch(API + "/comfyui/workflow-layouts"' in comfyui_js
    assert 'apiFetch(API + "/comfyui/workflows/export-current"' in comfyui_js
    assert 'apiFetch(API + "/comfyui/workflows/import"' in comfyui_js
    assert 'apiFetch(API + `/comfyui/workflows/${encodeURIComponent(presetId)}/run`' in comfyui_js
    assert 'apiFetch(API + `/admin/comfyui/workflows/${encodeURIComponent(presetId)}/publish-official`' in comfyui_js
    assert ".comfyui-workflow-grid" in css
    assert ".comfyui-workflow-item" in css
    assert ".comfyui-workflow-layout-details" in css
    assert ".comfyui-workflow-chip.warn" in css
    assert ".comfyui-workflow-chip.bad" in css
    assert "@media (max-width: 860px)" in css
    visual_editor_html = (ROOT / "public" / "comfyui-workflow-editor.html").read_text(encoding="utf-8")
    visual_editor_js = (ROOT / "public" / "js" / "comfyui-workflow-editor.js").read_text(encoding="utf-8")
    visual_editor_css = (ROOT / "public" / "comfyui-workflow-editor.css").read_text(encoding="utf-8")
    assert 'data-add-node="VAEEncodeForInpaint"' in visual_editor_html
    assert 'data-add-node="ImagePadForOutpaint"' in visual_editor_html
    assert 'data-add-node="ControlNetApplyAdvanced"' in visual_editor_html
    assert 'id="importJsonFile"' in visual_editor_html
    assert 'id="validationPanel"' in visual_editor_html
    assert 'id="validationBadge"' in visual_editor_html
    assert 'id="nodeSearchInput"' in visual_editor_html
    assert 'data-port-node="' in visual_editor_js
    assert "const INPUT_KEY = \"hackme_comfyui_workflow_editor_input\";" in visual_editor_js
    assert "function stateFromPackage(payload)" in visual_editor_js
    assert "function importJsonFile(event)" in visual_editor_js
    assert "function workflowValidationIssues()" in visual_editor_js
    assert "function renderValidationPanel()" in visual_editor_js
    assert "function filterNodePalette()" in visual_editor_js
    assert "Custom node 需要 ComfyUI object_info 驗證" in visual_editor_js
    assert "const UNKNOWN_NODE_TYPE = \"__UnknownCustomNode__\";" in visual_editor_js
    assert "function isUnknownNode(node)" in visual_editor_js
    assert "function unknownInputSpecs(rawInputs = {})" in visual_editor_js
    assert "UnknownCustomNode" in visual_editor_js
    assert "function startConnection(event)" in visual_editor_js
    assert "function completeConnection(event)" in visual_editor_js
    assert "function portPoint(nodeId, kind, name)" in visual_editor_js
    assert "function addEdge({ from, output, to, input })" in visual_editor_js
    assert "data-delete-edge" in visual_editor_js
    assert ".edge-path.temp" in visual_editor_css
    assert ".edge-path.warn" in visual_editor_css
    assert ".edge-list-row" in visual_editor_css
    assert ".wf-node.unknown" in visual_editor_css
    assert ".dependency-list" in visual_editor_css
    assert ".validation-issue-list" in visual_editor_css
    assert ".palette-search" in visual_editor_css
    assert ".tool-grid button.is-hidden" in visual_editor_css
    assert ".port.output.connecting" in visual_editor_css
    assert ".port.input.compatible" in visual_editor_css
    assert "function bindComfyuiAdvancedUi()" in comfyui_js
    assert "function updateComfyuiModeNote(modeOverride = null)" in comfyui_js
    assert 'badge.textContent = normalizedMode === "local" ? "本地模式" : (normalizedMode === "diffusers" ? "Diffusers 模式" : "雲端 / 遠端模式");' in comfyui_js
    assert 'detail.textContent = comfyuiConnectionModeDetail(normalizedMode);' in comfyui_js
    assert '目前是本地模式：可由 root 啟動 / 停止本地 ComfyUI' in comfyui_js
    assert '目前是雲端 / 遠端模式：此頁會直接呼叫遠端 ComfyUI API 生圖' in comfyui_js
    assert '目前是 Diffusers 模式：後端會直接載入 Hugging Face repo 生圖' in comfyui_js
    assert "function pollComfyuiJobUntilDone(jobId, controller, timeoutSeconds, options = {})" in comfyui_js
    assert "function pollComfyuiModelDownloadJob(jobId)" in comfyui_js
    assert "function comfyuiStorageWarningText(payload = {})" in comfyui_js
    assert "const COMFYUI_GENERATION_TIMEOUT_SECONDS = 0;" in comfyui_js
    assert "不設最長等待上限" in comfyui_js
    assert "const COMFYUI_QUEUE_TIMEOUT_EXTENSION_SECONDS = 1800;" in comfyui_js
    assert "function extendComfyuiDeadlineForQueue(deadline, startedAt)" in comfyui_js
    assert "function setComfyuiModelDownloadProgress" in comfyui_js
    assert "function comfyuiRequestPayloadExtras()" in comfyui_js
    assert '"async_progress": True' not in comfyui_js
    assert "async_progress: true" in comfyui_js
    assert 'data-comfyui-lora-strength-model="${index}"' in comfyui_js
    assert 'data-comfyui-lora-strength-clip="${index}"' in comfyui_js
    assert "function updateComfyuiSelectedLoraStrength(input, field)" in comfyui_js
    assert "function fillComfyuiVaeSelect(values = [])" in comfyui_js
    assert "function renderComfyuiEmbeddingShortcuts(values = [])" in comfyui_js
    assert "function insertComfyuiEmbeddingToken(name)" in comfyui_js
    assert "function removeComfyuiEmbeddingTokenFromInput(input, name)" in comfyui_js
    assert "function comfyuiEmbeddingTokenVariants(name)" in comfyui_js
    assert "插入 / 移除" in comfyui_js
    assert "function removeComfyuiPromptTerms(terms = [], { promptType = \"prompt\" } = {})" in comfyui_js
    assert "function clearSelectedComfyuiLoras()" in comfyui_js
    assert "function removeComfyuiSelectedLoraByIndex(index)" in comfyui_js
    assert "function renderComfyuiHistory()" in comfyui_js
    assert 'apiFetch(API + "/comfyui/history"' in comfyui_js
    assert 'apiFetch(API + "/comfyui/image-preview"' in comfyui_js
    assert "function currentComfyuiPromptTypeForInsertion()" in comfyui_js
    assert "function bindComfyuiPromptCursorTracking()" in comfyui_js
    assert "function applyComfyuiPromptTerms(terms = [])" in comfyui_js
    assert "function renderComfyuiCivitaiTrainedWords(versionId)" in comfyui_js
    assert "function renderComfyuiCivitaiSearchResults(results)" in comfyui_js
    assert "function searchComfyuiCivitaiModels()" in comfyui_js
    assert "function useComfyuiCivitaiSearchResult(modelId)" in comfyui_js
    assert "comfyui-civitai-search-thumb" in comfyui_js
    assert "開啟 Civitai 頁面" in comfyui_js
    assert ".comfyui-civitai-search-thumb img" in css
    assert ".comfyui-civitai-search-link a" in css
    assert 'const COMFYUI_VAE_BUILTIN = "__checkpoint_builtin__";' in comfyui_js
    assert 'vae: vae === COMFYUI_VAE_BUILTIN ? "" : vae,' in comfyui_js
    assert 'fillComfyuiVaeSelect(json.vaes || []);' in comfyui_js
    assert 'renderComfyuiEmbeddingShortcuts(json.embeddings || []);' in comfyui_js
    assert 'comfyuiLoraDetails = json.lora_details' in comfyui_js
    assert "function pruneUnsupportedComfyuiSelectedLoras" in comfyui_js
    assert "function comfyuiLoraCompatibilityHint(name)" in comfyui_js
    assert "可能需要搭配同系列模型" in comfyui_js
    assert "若模型不相容，ComfyUI 可能產圖失敗或效果異常" in comfyui_js
    assert "comfyui-lora-compat-hint" in comfyui_js
    assert ".comfyui-lora-compat-hint" in css
    assert 'detail.supported !== true' not in comfyui_js
    assert 'const insertedTerms = applyComfyuiPromptTerms(detail.trained_words || []);' in comfyui_js
    assert 'const triggerText = insertedTerms.length' in comfyui_js
    assert '並自動補上 trigger words' in comfyui_js
    assert 'clearSelectedComfyuiLoras();' in comfyui_js
    assert 'removeComfyuiSelectedLoraByIndex(index);' in comfyui_js
    assert 'removeComfyuiPromptTerms(removableTerms, { promptType: "prompt" });' in comfyui_js
    assert 'normalized.includes("negative") || normalized.includes("neg")' not in comfyui_js
    assert "comfyuiPromptTypeLabel(promptType)" in comfyui_js
    assert '已把 ${cleanName} 插入${comfyuiPromptTypeLabel(promptType)}提示詞。' in comfyui_js
    assert '已從${comfyuiPromptTypeLabel(promptType)}提示詞移除 ${cleanName}。' in comfyui_js
    assert '<embeddings:' in comfyui_js
    assert "trigger words" in comfyui_js
    assert 'comfyuiEffectiveConnectionMode() !== "local"' in comfyui_js
    assert 'currentUser === "root"' in comfyui_js
    assert 'return true;' in comfyui_js
    assert 'tab.disabled = false;' in comfyui_js
    assert "不使用 LoRA（可略過）" in comfyui_js
    assert "scheduleComfyuiLocalStartPolling" in comfyui_js
    assert "function inspectComfyuiCivitaiModel()" in comfyui_js
    assert "function onComfyuiCivitaiVersionChange()" in comfyui_js
    assert "function updateComfyuiConnectionModeFields()" in admin_js
    assert "s-comfyui-connection-mode" in bootstrap_js
    assert "s-comfyui-civitai-api-key" in admin_js
    assert "下載會寫入本站設定的 ComfyUI models 目錄" in admin_js
    assert "startLocalComfyui" in bootstrap_js
    assert "searchComfyuiCivitaiModels" in comfyui_js
    assert "inspectComfyuiCivitaiModel" in bootstrap_js
    assert "stopLocalComfyui" in bootstrap_js
    assert "json.starting" in admin_js
    assert "scheduleComfyuiLocalStartPolling({ attemptsLeft = 120, delayMs = 5000 }" in comfyui_js
    assert 'apiFetch(API + "/comfyui/interrupt"' in comfyui_js
    assert 'apiFetch(API + "/comfyui/save"' in comfyui_js
    assert 'apiFetch(API + "/comfyui/discard"' in comfyui_js
    assert 'source_file_not_deleted' in comfyui_js
    assert 'apiFetch(API + "/comfyui/share"' in comfyui_js
    assert 'apiFetch(API + "/comfyui/status"' in comfyui_js
    assert "function loadComfyuiLastSettings()" in comfyui_js
    assert 'let comfyuiMaxBatchSize = 1;' in comfyui_js
    assert 'let comfyuiBillingQuote = null;' in comfyui_js
    assert 'function applyComfyuiRuntimeLimits(payload = {})' in comfyui_js
    assert "非 root 帳號成功產圖後每張扣" in comfyui_js
    assert 'setComfyuiIdleSuspend("comfyui_generate", !!busy, `${isComfyuiDiffusersMode() ? "Diffusers" : "ComfyUI"} 產圖中`);' in comfyui_js
    assert "function confirmComfyuiBilling(payload)" in comfyui_js
    assert "function preflightComfyuiBilling(payload, runCount, billingConfirmation)" in comfyui_js
    assert "function comfyuiRunCount()" in comfyui_js
    assert 'if (currentUser === "root") return { confirmed: true, required: false };' in comfyui_js
    assert "window.confirm" in comfyui_js
    assert "LoRA 加價" in comfyui_js
    assert 'comfyuiSelectedLoras.length >= COMFYUI_MAX_LORAS' in comfyui_js
    assert "batchSize * runCount" in comfyui_js
    assert "for (let requestIndex = 0; requestIndex < totalRequests; requestIndex += 1)" in comfyui_js
    assert "setComfyuiMessage(`正在產生第 ${requestIndex + 1} / ${totalRequests} 張圖片...`, true)" in comfyui_js
    assert "confirm_billing: billingConfirmation.required" in comfyui_js
    assert "json.billing?.charged" in comfyui_js
    assert 'const batchSize = Math.max(1, Math.min(comfyuiMaxBatchSize, comfyuiNumberValue("comfyui-batch-size", 1)));' in comfyui_js
    assert "ui_batch_size: batchSize" in comfyui_js
    assert "comfyuiGeneratedImages" in comfyui_js
    assert "renderComfyuiGeneratedImages" in comfyui_js
    assert 'savePath.value = `/output/${comfyuiCurrentImage.image_ref.filename}`;' in comfyui_js
    assert 'placeholder="/output/圖片.png"' in index_html
    assert "COMFYUI_DRAFT_FIELD_IDS" in comfyui_js
    assert 'comfyuiUserStorageKey("comfyui:draft")' in comfyui_js
    assert "bindComfyuiDraftPersistence" in comfyui_js
    assert "restoreComfyuiDraft()" in comfyui_js
    assert 'album_id: selectedComfyuiAlbumId()' in comfyui_js
    assert "startComfyuiProgress(COMFYUI_GENERATION_TIMEOUT_SECONDS * runCount)" in comfyui_js
    assert "stopComfyuiProgress({ complete: true })" in comfyui_js
    assert "comfyuiGenerateAbortController.abort()" in comfyui_js
    assert "comfyuiShareGenerationPayload" in comfyui_js
    assert "payload.seed = comfyuiCurrentImage.seed" in comfyui_js
    assert 'id="comfyui-width" min="64" max="2048" step="8" value="1024"' in index_html
    assert 'id="comfyui-height" min="64" max="2048" step="8" value="1024"' in index_html
    assert 'id="s-comfyui-default-width"' in index_html
    assert 'id="s-comfyui-default-height"' in index_html
    assert "comfyuiDefaultWidth = 1024" in comfyui_js
    assert "comfyuiDefaultHeight = 1024" in comfyui_js
    assert 'if ($("s-comfyui-default-width"))' in admin_js
    assert "comfyui_default_width" in admin_js
    assert "interruptComfyuiGeneration" in bootstrap_js
    assert 'if (comfyuiLoadDraftBtn) comfyuiLoadDraftBtn.addEventListener("click", loadComfyuiLastSettings);' in bootstrap_js
    assert "bindComfyuiDraftPersistence" in bootstrap_js
    assert "bindComfyuiAdvancedUi" in bootstrap_js
    assert 'apiFetch(API + "/root/comfyui/test-connection"' in admin_js
    assert 'if (comfyuiTestConnectionBtn) comfyuiTestConnectionBtn.addEventListener("click", testComfyuiConnection);' in bootstrap_js
    assert 'shareComfyuiToCommunity' in bootstrap_js
    assert "comfyui-image:" in community_js
    assert "communityPreviewContentUrl" in community_js
    assert "csrf_token=${encodeURIComponent(token)}" not in community_js
    assert "/cloud-drive/files/${encodeURIComponent(fileId)}/preview/content" in community_js
    assert "/js/25-community.js?v=20260518-inline-media" in index_html
    assert 'isComfyuiAvailableForNavigation' in admin_js
    assert '"feature_comfyui_enabled": False' in platform_settings_py
    assert 'DEFAULT_COMFYUI_REMOTE_API_URL = os.environ.get("COMFYUI_API_URL", "http://127.0.0.1:8188").rstrip("/")' in comfyui_settings_py
    assert '"comfyui_remote_api_url": DEFAULT_COMFYUI_REMOTE_API_URL' in comfyui_settings_py
    assert '"comfyui_api_host": os.environ.get("COMFYUI_API_HOST", "localhost")' in comfyui_settings_py
    assert '"comfyui_api_port": DEFAULT_COMFYUI_PORT' in comfyui_settings_py
    assert '"comfyui_max_batch_size": DEFAULT_COMFYUI_MAX_BATCH_SIZE' in comfyui_settings_py
    assert '"comfyui_default_width": DEFAULT_COMFYUI_WIDTH' in comfyui_settings_py
    assert '"comfyui_default_height": DEFAULT_COMFYUI_HEIGHT' in comfyui_settings_py
    assert "/api/comfyui/models" in smoke
