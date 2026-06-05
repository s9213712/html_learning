import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass

try:
    import websocket
except Exception:  # pragma: no cover - optional import fallback
    websocket = None

from services.comfyui.constants import (
    CONTROLNET_TYPE_DEFINITIONS,
    GENERATION_MODE_DEFINITIONS,
    detect_model_families,
)
from services.comfyui import execution as comfy_execution
from services.comfyui import files as comfy_files
from services.comfyui.workflow import builder as workflow_builder


class ComfyUIError(RuntimeError):
    pass


_OBJECT_INFO_TTL_SECONDS = 300.0
_OBJECT_INFO_CACHE = {}
_OBJECT_INFO_LOCK = threading.RLock()


def _object_info_timeout_seconds(default_timeout):
    try:
        configured = float(os.environ.get("HACKME_COMFYUI_OBJECT_INFO_TIMEOUT_SECONDS", "8"))
    except Exception:
        configured = 8.0
    try:
        default = float(default_timeout or 30)
    except Exception:
        default = 30.0
    return max(1.0, min(configured, default, 30.0))


def _node_input_options_from_info(info, node_class, input_name):
    node = info.get(node_class) if isinstance(info, dict) else None
    required = ((node or {}).get("input") or {}).get("required") or {}
    raw_values = required.get(input_name) or []
    values = []
    if isinstance(raw_values, list) and raw_values:
        first = raw_values[0]
        if isinstance(first, list):
            values = first
        elif isinstance(first, str) and len(raw_values) > 1 and isinstance(raw_values[1], dict):
            options = raw_values[1].get("options") or []
            if isinstance(options, list):
                values = options
    return [str(item) for item in values if str(item).strip()]


def _clean_option_list(values):
    cleaned = []
    for item in values or []:
        if isinstance(item, dict):
            item = (
                item.get("name")
                or item.get("file_name")
                or item.get("filename")
                or item.get("value")
                or item.get("file_path")
                or ""
            )
        text = str(item or "").strip()
        if "/" in text or "\\" in text:
            text = text.replace("\\", "/").rsplit("/", 1)[-1]
        if text:
            cleaned.append(text)
    return cleaned


def _model_folder_api_options(values):
    options = []
    for item in values or []:
        if isinstance(item, dict):
            item = (
                item.get("name")
                or item.get("file_name")
                or item.get("filename")
                or item.get("value")
                or item.get("path")
                or item.get("file_path")
                or ""
            )
        text = str(item or "").strip()
        if text:
            options.append(text)
    return options


@dataclass
class ComfyUIImage:
    filename: str
    subfolder: str
    type: str
    mime_type: str
    data: bytes


class ComfyUIClient:
    def __init__(self, base_url="http://localhost:8192", timeout=30):
        self.base_url = str(base_url or "http://localhost:8192").rstrip("/")
        self.timeout = int(timeout or 30)

    def _url(self, path):
        return f"{self.base_url}{path}"

    def _ws_url(self):
        parsed = urllib.parse.urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        netloc = parsed.netloc or parsed.path
        path = parsed.path.rstrip("/")
        return urllib.parse.urlunparse((scheme, netloc, path, "", "", ""))

    def _json_request(self, path, *, method="GET", payload=None, timeout=None, allow_non_json=False):
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._url(path), data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    if allow_non_json:
                        return {"raw": raw}
                    raise ComfyUIError("ComfyUI 回應不是 JSON") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ComfyUIError("ComfyUI 連線逾時") from exc
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
                if raw.strip():
                    detail = raw.strip()[:800]
            except Exception:
                detail = ""
            suffix = f"：{detail}" if detail else f"：{getattr(exc, 'reason', exc)}"
            raise ComfyUIError(f"ComfyUI HTTP {exc.code}{suffix}") from exc
        except urllib.error.URLError as exc:
            raise ComfyUIError(f"ComfyUI 連線失敗：{getattr(exc, 'reason', exc)}") from exc

    def _multipart_request(self, path, *, fields=None, files=None, timeout=None):
        boundary = f"----HackmeWebComfyUI{uuid.uuid4().hex}"
        body = bytearray()
        for key, value in (fields or {}).items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")
        for item in files or []:
            if not isinstance(item, dict):
                continue
            field_name = str(item.get("field") or "image")
            filename = str(item.get("filename") or "upload.bin")
            content_type = str(item.get("content_type") or "application/octet-stream")
            data = item.get("data") or b""
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8")
            )
            body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            body.extend(data)
            body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("utf-8"))
        headers = {
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        req = urllib.request.Request(self._url(path), data=bytes(body), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ComfyUIError("ComfyUI 回應不是 JSON") from exc
        except (TimeoutError, socket.timeout) as exc:
            raise ComfyUIError("ComfyUI 連線逾時") from exc
        except urllib.error.URLError as exc:
            raise ComfyUIError(f"ComfyUI 連線失敗：{getattr(exc, 'reason', exc)}") from exc

    def _open_progress_socket(self, client_id, *, timeout=5):
        if websocket is None:
            raise ComfyUIError("缺少 websocket-client 套件，無法讀取 ComfyUI 即時進度")
        query = urllib.parse.urlencode({"clientId": str(client_id)})
        try:
            ws = websocket.create_connection(f"{self._ws_url()}/ws?{query}", timeout=timeout)
            ws.settimeout(0.25)
            return ws
        except Exception as exc:
            raise ComfyUIError(f"ComfyUI websocket 連線失敗：{exc}") from exc

    def _list_node_input_options(self, node_class, input_name):
        return _node_input_options_from_info(self.get_object_info(node_class), node_class, input_name)

    def get_model_folder_models(self, folder_name):
        folder = str(folder_name or "").strip().strip("/")
        if not folder:
            return []
        data = self._json_request(
            f"/api/models/{urllib.parse.quote(folder, safe='')}",
            timeout=_object_info_timeout_seconds(self.timeout),
        )
        if isinstance(data, list):
            return _model_folder_api_options(data)
        if isinstance(data, dict):
            for key in ("models", "files", "items"):
                values = data.get(key)
                if isinstance(values, list):
                    return _model_folder_api_options(values)
        return []

    def _list_model_loader_options(self, node_class, input_name, folder_name):
        options = self._list_node_input_options(node_class, input_name)
        if options:
            return options
        try:
            return self.get_model_folder_models(folder_name)
        except ComfyUIError:
            return []

    def get_object_info(self, node_class=None):
        path = "/object_info"
        if node_class:
            path = f"/object_info/{urllib.parse.quote(str(node_class))}"
        cache_key = (self.base_url, path)
        full_cache_key = (self.base_url, "/object_info")
        now = time.monotonic()
        if node_class:
            with _OBJECT_INFO_LOCK:
                cached_full = _OBJECT_INFO_CACHE.get(full_cache_key)
                if cached_full and (now - cached_full[0]) < _OBJECT_INFO_TTL_SECONDS:
                    full_info = cached_full[1]
                    node = full_info.get(str(node_class)) if isinstance(full_info, dict) else None
                    if isinstance(node, dict):
                        return {str(node_class): node}
        with _OBJECT_INFO_LOCK:
            cached = _OBJECT_INFO_CACHE.get(cache_key)
            if cached and (now - cached[0]) < _OBJECT_INFO_TTL_SECONDS:
                return cached[1]
        try:
            info = self._json_request(path, timeout=_object_info_timeout_seconds(self.timeout))
        except ComfyUIError:
            with _OBJECT_INFO_LOCK:
                cached = _OBJECT_INFO_CACHE.get(cache_key)
                if cached:
                    return cached[1]
                if node_class:
                    cached_full = _OBJECT_INFO_CACHE.get(full_cache_key)
                    full_info = cached_full[1] if cached_full else None
                    node = full_info.get(str(node_class)) if isinstance(full_info, dict) else None
                    if isinstance(node, dict):
                        return {str(node_class): node}
            raise
        if not isinstance(info, dict):
            raise ComfyUIError("ComfyUI object_info 回應格式不正確")
        with _OBJECT_INFO_LOCK:
            _OBJECT_INFO_CACHE[cache_key] = (now, info)
        return info

    def list_node_classes(self):
        return sorted(self.get_object_info().keys())

    def get_models(self):
        return self._list_node_input_options("CheckpointLoaderSimple", "ckpt_name")

    def get_loras(self):
        return self._list_node_input_options("LoraLoader", "lora_name")

    def get_vaes(self):
        return self._list_node_input_options("VAELoader", "vae_name")

    def get_embeddings(self):
        try:
            data = self._json_request("/embeddings")
        except ComfyUIError:
            return self._get_lora_manager_embeddings()
        if isinstance(data, list):
            values = data
        elif isinstance(data, dict):
            values = (
                data.get("embeddings")
                or data.get("embedding")
                or data.get("items")
                or data.get("textual_inversions")
                or data.get("textual_inversion")
                or []
            )
        else:
            values = []
        if not isinstance(values, list):
            values = []
        cleaned = _clean_option_list(values)
        return cleaned or self._get_lora_manager_embeddings()

    def _get_lora_manager_embeddings(self):
        values = []
        total_pages = 1
        for page in range(1, 51):
            path = f"/api/lm/embeddings/list?{urllib.parse.urlencode({'page': page, 'page_size': 200})}"
            try:
                data = self._json_request(path)
            except ComfyUIError:
                break
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("embeddings") or data.get("models") or []
                try:
                    total_pages = max(1, min(50, int(data.get("total_pages") or total_pages or 1)))
                except Exception:
                    total_pages = 1
            else:
                items = []
            if isinstance(items, list):
                values.extend(items)
            if page >= total_pages:
                break
        return _clean_option_list(values)

    def health_check(self, *, timeout=3):
        stats = self._json_request("/system_stats", timeout=timeout)
        if not isinstance(stats, dict):
            raise ComfyUIError("ComfyUI 狀態回應格式不正確")
        return {
            "ok": True,
            "base_url": self.base_url,
            "system": stats.get("system") or {},
        }

    def get_sampler_options(self):
        info = self._json_request("/object_info/KSampler")
        node = info.get("KSampler") if isinstance(info, dict) else None
        required = ((node or {}).get("input") or {}).get("required") or {}
        sampler = required.get("sampler_name") or []
        scheduler = required.get("scheduler") or []
        sampler_values = sampler[0] if isinstance(sampler, list) and sampler else []
        scheduler_values = scheduler[0] if isinstance(scheduler, list) and scheduler else []
        return {
            "samplers": [str(item) for item in sampler_values] if isinstance(sampler_values, list) else [],
            "schedulers": [str(item) for item in scheduler_values] if isinstance(scheduler_values, list) else [],
        }

    def get_controlnet_models(self):
        return self._list_model_loader_options("ControlNetLoader", "control_net_name", "controlnet")

    def get_upscale_models(self):
        return self._list_model_loader_options("UpscaleModelLoader", "model_name", "upscale_models")

    def get_latent_upscale_models(self):
        return self._list_model_loader_options("LatentUpscaleModelLoader", "model_name", "latent_upscale_models")

    def get_clip_vision_models(self):
        return self._list_model_loader_options("CLIPVisionLoader", "clip_name", "clip_vision")

    def get_capabilities(self):
        object_info = self.get_object_info()
        available_nodes = set(object_info.keys())
        models = _node_input_options_from_info(object_info, "CheckpointLoaderSimple", "ckpt_name")
        loras = _node_input_options_from_info(object_info, "LoraLoader", "lora_name") if "LoraLoader" in available_nodes else []
        vaes = _node_input_options_from_info(object_info, "VAELoader", "vae_name") if "VAELoader" in available_nodes else []
        diffusion_models = []
        for class_type in ("UNETLoader", "UnetLoaderGGUF", "UnetLoaderGGUFAdvanced"):
            if class_type in available_nodes:
                diffusion_models.extend(_node_input_options_from_info(object_info, class_type, "unet_name"))
        clip_models = []
        for class_type, input_name in (
            ("CLIPLoader", "clip_name"),
            ("CLIPLoaderGGUF", "clip_name"),
            ("DualCLIPLoader", "clip_name1"),
            ("DualCLIPLoader", "clip_name2"),
            ("DualCLIPLoaderGGUF", "clip_name1"),
            ("DualCLIPLoaderGGUF", "clip_name2"),
            ("TripleCLIPLoader", "clip_name1"),
            ("TripleCLIPLoader", "clip_name2"),
            ("TripleCLIPLoader", "clip_name3"),
            ("TripleCLIPLoaderGGUF", "clip_name1"),
            ("TripleCLIPLoaderGGUF", "clip_name2"),
            ("TripleCLIPLoaderGGUF", "clip_name3"),
        ):
            if class_type in available_nodes:
                clip_models.extend(_node_input_options_from_info(object_info, class_type, input_name))
        controlnet_models = _node_input_options_from_info(object_info, "ControlNetLoader", "control_net_name") if "ControlNetLoader" in available_nodes else []
        upscale_models = _node_input_options_from_info(object_info, "UpscaleModelLoader", "model_name") if "UpscaleModelLoader" in available_nodes else []
        latent_upscale_models = _node_input_options_from_info(object_info, "LatentUpscaleModelLoader", "model_name") if "LatentUpscaleModelLoader" in available_nodes else []
        clip_vision_models = _node_input_options_from_info(object_info, "CLIPVisionLoader", "clip_name") if "CLIPVisionLoader" in available_nodes else []
        if "ControlNetLoader" in available_nodes and not controlnet_models:
            try:
                controlnet_models = self.get_model_folder_models("controlnet")
            except ComfyUIError:
                controlnet_models = []
        if "UpscaleModelLoader" in available_nodes and not upscale_models:
            try:
                upscale_models = self.get_model_folder_models("upscale_models")
            except ComfyUIError:
                upscale_models = []
        if "LatentUpscaleModelLoader" in available_nodes and not latent_upscale_models:
            try:
                latent_upscale_models = self.get_model_folder_models("latent_upscale_models")
            except ComfyUIError:
                latent_upscale_models = []
        if "CLIPVisionLoader" in available_nodes and not clip_vision_models:
            try:
                clip_vision_models = self.get_model_folder_models("clip_vision")
            except ComfyUIError:
                clip_vision_models = []
        samplers = _node_input_options_from_info(object_info, "KSampler", "sampler_name")
        schedulers = _node_input_options_from_info(object_info, "KSampler", "scheduler")
        controlnet_types = {}
        for key, definition in CONTROLNET_TYPE_DEFINITIONS.items():
            preprocessor_candidates = list(definition.get("preprocessor_candidates") or [])
            available_preprocessors = [name for name in preprocessor_candidates if name in available_nodes]
            model_keywords = [keyword.lower() for keyword in definition.get("model_keywords") or []]
            matching_models = [
                model
                for model in controlnet_models
                if any(keyword in str(model).lower() for keyword in model_keywords)
            ]
            controlnet_types[key] = {
                "label": definition.get("label") or key,
                "available": bool(
                    {"ControlNetLoader", "ControlNetApplyAdvanced", "LoadImage"}.issubset(available_nodes)
                    and available_preprocessors
                    and matching_models
                ),
                "available_preprocessors": available_preprocessors,
                "default_preprocessor": next(
                    (
                        name
                        for name in [definition.get("default_preprocessor")] + preprocessor_candidates
                        if name in available_preprocessors
                    ),
                    "",
                ),
                "matching_models": matching_models,
            }
        return {
            "available_nodes": sorted(available_nodes),
            "models": models,
            "loras": loras,
            "vaes": vaes,
            "diffusion_models": sorted(set(diffusion_models)),
            "clip_models": sorted(set(clip_models)),
            "clip_vision_models": sorted(set(clip_vision_models)),
            "samplers": samplers,
            "schedulers": schedulers,
            "controlnet_models": controlnet_models,
            "upscale_models": upscale_models,
            "latent_upscale_models": latent_upscale_models,
            "controlnet_types": controlnet_types,
            "generation_modes": [
                {
                    "key": key,
                    "label": value.get("label") or key,
                    "available": True,
                    "workflow_only": bool(value.get("workflow_only")),
                    "output_kind": value.get("output_kind") or "image",
                    "source_kind": value.get("source_kind") or "",
                    "recommended_model_families": list(value.get("recommended_model_families") or []),
                }
                for key, value in GENERATION_MODE_DEFINITIONS.items()
            ],
            "model_families": detect_model_families([*models, *loras, *vaes, *diffusion_models, *clip_models, *clip_vision_models, *controlnet_models, *upscale_models, *latent_upscale_models]),
        }

    def upload_image_bytes(self, data, filename, *, image_type="input", overwrite=False, subfolder=""):
        return comfy_files.upload_image_bytes(
            self,
            data,
            filename,
            image_type=image_type,
            overwrite=overwrite,
            subfolder=subfolder,
            error_cls=ComfyUIError,
        )

    def _build_text_to_image_base(self, params):
        return workflow_builder.build_text_to_image_base(params)

    def _attach_controlnet(self, workflow, params, *, positive_ref, negative_ref, next_node_id):
        return workflow_builder.attach_controlnet(
            workflow,
            params,
            positive_ref=positive_ref,
            negative_ref=negative_ref,
            next_node_id=next_node_id,
            error_cls=ComfyUIError,
        )

    def build_text_to_image_workflow(self, params):
        return workflow_builder.build_text_to_image_workflow(params, error_cls=ComfyUIError)

    def build_image_to_image_workflow(self, params):
        return workflow_builder.build_image_to_image_workflow(params, error_cls=ComfyUIError)

    def build_inpaint_workflow(self, params):
        return workflow_builder.build_inpaint_workflow(params, error_cls=ComfyUIError)

    def build_outpaint_workflow(self, params):
        return workflow_builder.build_outpaint_workflow(params, error_cls=ComfyUIError)

    def build_upscale_workflow(self, params):
        return workflow_builder.build_upscale_workflow(params, error_cls=ComfyUIError)

    def build_generation_workflow(self, params):
        return workflow_builder.build_generation_workflow(params, error_cls=ComfyUIError)

    def queue_prompt_with_client_id(self, workflow, *, client_id=None, extra_data=None):
        return comfy_execution.queue_prompt_with_client_id(
            self,
            workflow,
            client_id=client_id,
            extra_data=extra_data,
            error_cls=ComfyUIError,
        )

    def queue_prompt(self, workflow, *, extra_data=None):
        return comfy_execution.queue_prompt(self, workflow, extra_data=extra_data, error_cls=ComfyUIError)

    def interrupt(self, *, timeout_seconds=None):
        return comfy_execution.interrupt(self, timeout_seconds=timeout_seconds)

    def delete_queue_items(self, prompt_ids, *, timeout_seconds=None):
        return comfy_execution.delete_queue_items(self, prompt_ids, timeout_seconds=timeout_seconds)

    def _emit_progress(self, progress_callback, snapshot):
        return comfy_execution.emit_progress(progress_callback, snapshot)

    def _apply_ws_message_to_progress(self, snapshot, message, prompt_id, *, workflow=None):
        return comfy_execution.apply_ws_message_to_progress(snapshot, message, prompt_id, workflow=workflow)

    def wait_for_images(
        self,
        prompt_id,
        *,
        timeout_seconds=1800,
        poll_interval=1.0,
        expected_count=1,
        websocket_conn=None,
        progress_callback=None,
    ):
        return comfy_execution.wait_for_images(
            self,
            prompt_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            expected_count=expected_count,
            websocket_conn=websocket_conn,
            progress_callback=progress_callback,
            error_cls=ComfyUIError,
            websocket_module=websocket,
        )

    def wait_for_first_image(self, prompt_id, *, timeout_seconds=1800, poll_interval=1.0):
        return comfy_execution.wait_for_first_image(
            self,
            prompt_id,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            error_cls=ComfyUIError,
            websocket_module=websocket,
        )

    def fetch_image(self, image_ref):
        return comfy_files.fetch_image(self, image_ref, error_cls=ComfyUIError, image_cls=ComfyUIImage)

    def fetch_file(self, file_ref):
        return comfy_files.fetch_file(self, file_ref, error_cls=ComfyUIError, file_cls=ComfyUIImage)

    def _local_dir_for_type(self, image_type, *, local_base_dir=None):
        return comfy_files.local_dir_for_type(
            image_type,
            error_cls=ComfyUIError,
            local_base_dir=local_base_dir,
        )

    def _safe_local_image_path(self, image_ref, *, local_base_dir=None):
        return comfy_files.safe_local_image_path(
            image_ref,
            error_cls=ComfyUIError,
            local_base_dir=local_base_dir,
        )

    def discard_image(self, image_ref, *, prompt_id=None, local_base_dir=None, allow_api_delete=True):
        return comfy_files.discard_image(
            self,
            image_ref,
            prompt_id=prompt_id,
            local_base_dir=local_base_dir,
            allow_api_delete=allow_api_delete,
            error_cls=ComfyUIError,
        )

    def generate_from_workflow(
        self,
        workflow,
        *,
        timeout_seconds=1800,
        expected_count=1,
        progress_callback=None,
        extra_data=None,
        fetch_outputs=True,
        wait_until_completed=False,
    ):
        return comfy_execution.generate_from_workflow(
            self,
            workflow,
            timeout_seconds=timeout_seconds,
            expected_count=expected_count,
            progress_callback=progress_callback,
            extra_data=extra_data,
            fetch_outputs=fetch_outputs,
            wait_until_completed=wait_until_completed,
            error_cls=ComfyUIError,
            websocket_module=websocket,
            image_fetcher=self.fetch_image,
        )

    def generate_image(self, params, *, timeout_seconds=1800, progress_callback=None, extra_data=None, fetch_outputs=True):
        return comfy_execution.generate_image(
            self,
            params,
            timeout_seconds=timeout_seconds,
            progress_callback=progress_callback,
            extra_data=extra_data,
            fetch_outputs=fetch_outputs,
            error_cls=ComfyUIError,
            build_generation_workflow_func=self.build_generation_workflow,
            generate_from_workflow_func=self.generate_from_workflow,
        )
