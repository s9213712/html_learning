#!/usr/bin/env python3
import argparse
import base64
import http.client
import http.cookiejar
import json
import secrets
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from io import BytesIO
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def png_rgba(width, height, rgba):
    r, g, b, a = [max(0, min(255, int(v))) for v in rgba]
    row = bytes([r, g, b, a]) * int(width)
    raw = b"".join(b"\x00" + row for _ in range(int(height)))

    def chunk(tag, payload):
        body = tag + payload
        return struct.pack("!I", len(payload)) + body + struct.pack("!I", zlib.crc32(body) & 0xFFFFFFFF)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack("!IIBBBBB", int(width), int(height), 8, 6, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(raw, level=9))
    iend = chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


SOURCE_PNG = png_rgba(64, 64, (90, 140, 230, 255))
MASK_PNG = png_rgba(64, 64, (255, 255, 255, 255))
CONTROL_PNG = png_rgba(64, 64, (0, 0, 0, 255))


class ProbeError(RuntimeError):
    pass


class JobExecutionError(ProbeError):
    def __init__(self, message, *, abort_receipt=None, payload=None):
        super().__init__(message)
        self.abort_receipt = abort_receipt if isinstance(abort_receipt, dict) else {}
        self.payload = payload if isinstance(payload, dict) else {}


class WebClient:
    def __init__(
        self,
        base_url,
        *,
        insecure=False,
        user_agent="hackme_web-comfyui-probe/1.0",
        request_timeout=10,
    ):
        self.base_url = str(base_url).rstrip("/")
        self.request_timeout = max(1.0, min(float(request_timeout or 10), 60.0))
        self.jar = http.cookiejar.CookieJar()
        handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
        if self.base_url.startswith("https://"):
            ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [("User-Agent", user_agent)]
        self.csrf_token = ""

    def _url(self, path):
        return f"{self.base_url}{path if str(path).startswith('/') else '/' + str(path)}"

    def fetch_csrf(self):
        payload = self.get_json("/api/csrf-token")
        token = str(payload.get("csrf_token") or "").strip()
        if not token:
            raise ProbeError("伺服器沒有回傳 csrf_token")
        self.csrf_token = token
        return token

    def login(self, username, password):
        self.fetch_csrf()
        payload = {"username": username, "password": password}
        data = self.post_json("/api/login", payload, allow_http_error=True, refresh_csrf=False)
        if not data.get("ok"):
            raise ProbeError(f"登入失敗：{data.get('msg') or 'unknown error'}")
        self.fetch_csrf()
        return data

    def _request(self, path, *, method="GET", body=None, headers=None, allow_http_error=False):
        def read_body(response):
            try:
                return response.read()
            except http.client.IncompleteRead as exc:
                return exc.partial or b""

        req = urllib.request.Request(self._url(path), data=body, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=self.request_timeout) as resp:
                raw = read_body(resp)
                return resp.status, raw, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            raw = read_body(exc)
            if not allow_http_error:
                raise
            return exc.code, raw, dict(exc.headers)

    def get_json(self, path, *, allow_http_error=False):
        status, raw, _headers = self._request(path, allow_http_error=allow_http_error)
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ProbeError(f"{path} 回應不是 JSON（HTTP {status}）") from exc
        if not isinstance(payload, dict):
            payload = {"ok": False, "msg": f"{path} 回應不是 JSON object", "raw": payload}
        payload["_http_status"] = status
        return payload

    def post_json(self, path, payload, *, allow_http_error=False, refresh_csrf=True):
        if refresh_csrf:
            self.fetch_csrf()
        headers = {"Content-Type": "application/json"}
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        status, raw, _headers = self._request(
            path,
            method="POST",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            allow_http_error=allow_http_error,
        )
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ProbeError(f"{path} 回應不是 JSON（HTTP {status}）") from exc
        if not isinstance(data, dict):
            data = {"ok": False, "msg": f"{path} 回應不是 JSON object", "raw": data}
        data["_http_status"] = status
        return data

    def post_multipart(self, path, *, fields=None, files=None, allow_http_error=False, refresh_csrf=True):
        if refresh_csrf:
            self.fetch_csrf()
        boundary = f"----HackmeWebProbe{int(time.time() * 1000)}"
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
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        status, raw, _headers = self._request(
            path,
            method="POST",
            body=bytes(body),
            headers=headers,
            allow_http_error=allow_http_error,
        )
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ProbeError(f"{path} 回應不是 JSON（HTTP {status}）") from exc
        if not isinstance(data, dict):
            data = {"ok": False, "msg": f"{path} 回應不是 JSON object", "raw": data}
        data["_http_status"] = status
        return data


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _probe_result(name, *, ok, status, detail="", payload=None):
    return {
        "name": name,
        "ok": bool(ok),
        "status": status,
        "detail": detail,
        "payload": payload or {},
        "checked_at": _now(),
    }


def _job(payload):
    job = (payload or {}).get("job")
    return job if isinstance(job, dict) else {}


def _job_result(payload):
    result = _job(payload).get("result")
    return result if isinstance(result, dict) else {}


def _job_error(payload):
    return str((payload or {}).get("msg") or _job(payload).get("error") or "")


TERMINAL_JOB_STATES = frozenset({"completed", "error", "failed", "cancelled"})


def bounded_interrupt(client, *, job_id="", reason="probe_failure", grace_seconds=30, poll_interval=0.5):
    started = time.monotonic()
    receipt = {
        "attempted": True,
        "job_id": str(job_id or ""),
        "reason": str(reason or "probe_failure"),
        "request_ok": False,
        "backend_interrupted": False,
        "terminal_observed": False,
        "terminal_status": "",
        "poll_count": 0,
        "errors": [],
    }
    try:
        response = client.post_json(
            "/api/comfyui/interrupt",
            {"job_id": str(job_id or ""), "reason": str(reason or "probe_failure")},
            allow_http_error=True,
            refresh_csrf=False,
        )
        interrupt = response.get("interrupt") if isinstance(response.get("interrupt"), dict) else {}
        receipt["request"] = response
        receipt["request_ok"] = bool(response.get("_http_status") == 200 and response.get("ok") is True)
        receipt["backend_interrupted"] = bool(interrupt.get("backend_interrupted"))
        receipt["backend_queue_absence_verified"] = bool(
            interrupt.get("queue_absence_verified") is True
            and interrupt.get("queue_depth") == 0
        )
    except Exception as exc:
        receipt["errors"].append(f"interrupt_request: {exc}")

    if job_id:
        deadline = time.monotonic() + max(1.0, min(float(grace_seconds), 120.0))
        while time.monotonic() < deadline:
            receipt["poll_count"] += 1
            try:
                payload = client.get_json(
                    f"/api/comfyui/jobs/{urllib.parse.quote(str(job_id))}",
                    allow_http_error=True,
                )
                receipt["last_job"] = payload
                status = str(_job(payload).get("status") or "").strip().lower()
                if status in TERMINAL_JOB_STATES:
                    receipt["terminal_observed"] = True
                    receipt["terminal_status"] = status
                    break
            except Exception as exc:
                receipt["errors"].append(f"terminal_poll: {exc}")
            time.sleep(max(0.05, float(poll_interval)))
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    receipt["bounded_stop_acknowledged"] = bool(
        receipt["request_ok"]
        and receipt["backend_interrupted"]
        and receipt["terminal_observed"]
        and not receipt["errors"]
    )
    # A terminal site job is not proof that its ComfyUI prompt left the
    # backend queue.  Keep the receipt non-exact until the server exposes an
    # explicit queue-absence attestation (the formal parent monitor performs
    # that independent direct-backend check today).
    receipt["exact"] = bool(
        receipt["bounded_stop_acknowledged"]
        and receipt.get("backend_queue_absence_verified") is True
    )
    receipt["ok"] = receipt["exact"]
    return receipt


def wait_for_job(client, job_id, *, timeout_seconds=180, cancel_grace_seconds=30):
    job_id = str(job_id or "").strip()
    if not job_id:
        raise JobExecutionError("ComfyUI generation response 缺少 job_id")
    deadline = time.monotonic() + max(1.0, min(float(timeout_seconds), 3600.0))
    last = None
    while time.monotonic() < deadline:
        try:
            payload = client.get_json(
                f"/api/comfyui/jobs/{urllib.parse.quote(job_id)}",
                allow_http_error=True,
            )
        except Exception as exc:
            receipt = bounded_interrupt(
                client,
                job_id=job_id,
                reason="job_poll_exception",
                grace_seconds=cancel_grace_seconds,
            )
            raise JobExecutionError(
                f"輪詢 ComfyUI job {job_id} 失敗：{exc}",
                abort_receipt=receipt,
                payload=last,
            ) from exc
        last = payload
        if payload.get("_http_status") != 200 or payload.get("ok") is not True:
            receipt = bounded_interrupt(
                client,
                job_id=job_id,
                reason="job_poll_invalid_response",
                grace_seconds=cancel_grace_seconds,
            )
            raise JobExecutionError(
                f"ComfyUI job {job_id} 狀態回應無效：{json.dumps(payload, ensure_ascii=False)[:400]}",
                abort_receipt=receipt,
                payload=payload,
            )
        job = _job(payload)
        if str(job.get("job_id") or "") != job_id:
            receipt = bounded_interrupt(
                client,
                job_id=job_id,
                reason="job_id_mismatch",
                grace_seconds=cancel_grace_seconds,
            )
            raise JobExecutionError(
                f"ComfyUI job correlation mismatch：expected={job_id}, actual={job.get('job_id')!r}",
                abort_receipt=receipt,
                payload=payload,
            )
        status = str(job.get("status") or "").strip().lower()
        if status in TERMINAL_JOB_STATES:
            return payload
        time.sleep(0.8)
    receipt = bounded_interrupt(
        client,
        job_id=job_id,
        reason="job_timeout",
        grace_seconds=cancel_grace_seconds,
    )
    raise JobExecutionError(
        f"等待 ComfyUI job {job_id} 完成逾時；最後狀態：{json.dumps(last or {}, ensure_ascii=False)[:400]}",
        abort_receipt=receipt,
        payload=last,
    )


def minimal_generate_json(model_name, *, mode="txt2img", upscale_model="", probe_run_id=""):
    run_id = str(probe_run_id or "").strip()
    payload = {
        "generation_mode": mode,
        "model": model_name,
        "prompt": f"hackme_web {mode} probe {run_id}".strip(),
        "negative_prompt": "",
        "width": 512,
        "height": 512,
        "steps": 2,
        "cfg": 4.0,
        "sampler_name": "euler",
        "scheduler": "normal",
        "seed": 123,
        "batch_size": 1,
        "confirm_billing": True,
        "async_progress": True,
        "filename_prefix": f"hackme_feature_probe_{run_id}_{mode}".strip("_"),
    }
    if upscale_model:
        payload["upscale_model"] = upscale_model
    return payload


def select_checkpoint_model(models_payload, requested_model):
    """Return only an explicitly requested, exactly installed checkpoint.

    A feature probe can enqueue several GPU-heavy jobs.  Selecting the first
    checkpoint advertised by a backend makes that workload depend on remote
    ordering and can unexpectedly load a much larger model.  Keep the
    selection contract deliberately strict: callers must name the checkpoint
    and the site inventory must advertise that exact string.
    """

    requested = str(requested_model or "").strip()
    if not requested:
        raise ProbeError("必須透過 --model 明確指定 checkpoint；禁止自動選擇第一個模型")
    if not isinstance(models_payload, dict):
        raise ProbeError("ComfyUI models 回應格式不正確")
    if models_payload.get("_http_status") != 200 or models_payload.get("ok") is not True:
        raise ProbeError(
            "ComfyUI models inventory 不可用："
            f"HTTP {models_payload.get('_http_status')}, ok={models_payload.get('ok')!r}"
        )
    raw_models = models_payload.get("models")
    if not isinstance(raw_models, list) or not raw_models:
        raise ProbeError("ComfyUI models inventory 沒有 checkpoint model")
    if any(not isinstance(item, str) or not item.strip() for item in raw_models):
        raise ProbeError("ComfyUI models inventory 含有非字串或空白 checkpoint，拒絕選模")
    available = [item.strip() for item in raw_models]
    if requested not in available:
        raise ProbeError(
            "明確指定的 checkpoint 不在 ComfyUI models inventory；"
            f"requested={requested!r}, available_count={len(available)}"
        )
    return requested


def select_exact_inventory_value(models_payload, requested_value, *, inventory_key, label):
    requested = str(requested_value or "").strip()
    if not requested:
        raise ProbeError(f"必須明確指定 {label}；禁止自動選擇第一個項目")
    if not isinstance(models_payload, dict):
        raise ProbeError("ComfyUI models 回應格式不正確")
    if models_payload.get("_http_status") != 200 or models_payload.get("ok") is not True:
        raise ProbeError(f"ComfyUI {label} inventory 不可用")
    values = models_payload.get(inventory_key)
    if not isinstance(values, list) or not values:
        raise ProbeError(f"ComfyUI 沒有可用的 {label}")
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise ProbeError(f"ComfyUI {label} inventory 格式不正確")
    available = [item.strip() for item in values]
    if requested not in available:
        raise ProbeError(
            f"明確指定的 {label} 不在 inventory；"
            f"requested={requested!r}, available_count={len(available)}"
        )
    return requested


def select_controlnet_dependencies(models_payload, *, controlnet_type, model_name, preprocessor):
    control_type = str(controlnet_type or "").strip().lower()
    type_map = models_payload.get("controlnet_types") if isinstance(models_payload, dict) else None
    type_info = type_map.get(control_type) if isinstance(type_map, dict) else None
    if not isinstance(type_info, dict) or type_info.get("available") is not True:
        raise ProbeError(f"ControlNet type {control_type or '-'} 不可用")
    model = str(model_name or "").strip()
    processor = str(preprocessor or "").strip()
    if not model or not processor:
        raise ProbeError("必須明確指定 --controlnet-model 與 --controlnet-preprocessor；禁止後端自動挑第一個")
    matching_models = type_info.get("matching_models")
    processors = type_info.get("available_preprocessors")
    if not isinstance(matching_models, list) or model not in matching_models:
        raise ProbeError(f"ControlNet model 不在 {control_type} exact inventory：{model!r}")
    if not isinstance(processors, list) or processor not in processors:
        raise ProbeError(f"ControlNet preprocessor 不在 {control_type} exact inventory：{processor!r}")
    return {
        "type": control_type,
        "model_name": model,
        "preprocessor": processor,
    }


def _image_ref_key(image_ref):
    if not isinstance(image_ref, dict):
        return None
    filename = str(image_ref.get("filename") or "").strip()
    if not filename:
        return None
    return (
        filename,
        str(image_ref.get("subfolder") or "").strip(),
        str(image_ref.get("type") or "output").strip().lower(),
    )


def _validate_preview(client, *, image_ref, expected_size):
    preview = client.post_json(
        "/api/comfyui/image-preview",
        {"image_ref": image_ref},
        allow_http_error=True,
    )
    image = preview.get("image") if isinstance(preview.get("image"), dict) else {}
    if preview.get("_http_status") != 200 or preview.get("ok") is not True:
        raise ProbeError(f"output preview 讀取失敗：{preview}")
    if _image_ref_key(image.get("image_ref")) != _image_ref_key(image_ref):
        raise ProbeError("output preview image_ref correlation mismatch")
    mime_type = str(image.get("mime_type") or "").strip().lower()
    if mime_type not in {"image/png", "image/jpeg", "image/webp"}:
        raise ProbeError(f"output preview MIME 不支援或缺失：{mime_type!r}")
    try:
        size_bytes = int(image.get("size_bytes") or 0)
    except (TypeError, ValueError):
        size_bytes = 0
    data_url = str(image.get("data_url") or "")
    prefix = f"data:{mime_type};base64,"
    if size_bytes <= 0 or not data_url.startswith(prefix):
        raise ProbeError("output preview 缺少可解碼的非空圖片資料")
    try:
        raw = base64.b64decode(data_url[len(prefix):], validate=True)
    except Exception as exc:
        raise ProbeError(f"output preview base64 無法解碼：{exc}") from exc
    if len(raw) != size_bytes or (int(expected_size or 0) > 0 and len(raw) != int(expected_size)):
        raise ProbeError(
            "output preview size correlation mismatch："
            f"preview={len(raw)}, declared={size_bytes}, job={expected_size}"
        )
    if mime_type == "image/png" and not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ProbeError("output preview 宣稱 PNG 但 signature 不正確")
    if mime_type == "image/jpeg" and not (raw.startswith(b"\xff\xd8") and raw.endswith(b"\xff\xd9")):
        raise ProbeError("output preview 宣稱 JPEG 但 signature 不正確")
    if mime_type == "image/webp" and not (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"):
        raise ProbeError("output preview 宣稱 WebP 但 signature 不正確")
    expected_format = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}[mime_type]
    try:
        from PIL import Image

        with Image.open(BytesIO(raw)) as candidate:
            candidate.verify()
        with Image.open(BytesIO(raw)) as decoded:
            decoded.load()
            detected_format = str(decoded.format or "").upper()
            width, height = decoded.size
    except Exception as exc:
        raise ProbeError(f"output preview 無法由 image decoder 完整解析：{exc}") from exc
    if detected_format != expected_format or width < 1 or height < 1:
        raise ProbeError(
            "output preview decoder format/dimension mismatch："
            f"format={detected_format or '-'}, expected={expected_format}, size={width}x{height}"
        )
    return {
        "image_ref": image_ref,
        "mime_type": mime_type,
        "size_bytes": len(raw),
        "decoded": True,
        "decoder": "Pillow.verify+load",
        "format": detected_format,
        "width": int(width),
        "height": int(height),
    }


def validate_terminal_generation(
    client,
    terminal_payload,
    *,
    job_id,
    step_name,
    expected_mode,
    expected_model,
    expected_prompt,
    baseline_history_ids,
    expected_inputs=None,
    expected_controlnet=None,
    expected_upscale_model="",
):
    job = _job(terminal_payload)
    if str(job.get("job_id") or "") != str(job_id):
        raise ProbeError(f"{step_name}: terminal job_id correlation mismatch")
    if str(job.get("status") or "").strip().lower() != "completed":
        raise ProbeError(f"{step_name}: job terminal state 不是 completed：{job.get('status')!r}")
    result = _job_result(terminal_payload)
    try:
        history_id = int(result.get("history_id") or 0)
    except (TypeError, ValueError):
        history_id = 0
    if history_id <= 0 or history_id in set(baseline_history_ids or set()):
        raise ProbeError(f"{step_name}: job result 缺少全新的 numeric history_id")
    images = result.get("images") if isinstance(result.get("images"), list) else []
    if not images:
        raise ProbeError(f"{step_name}: completed job 沒有 output images")
    output_keys = []
    prompt_ids = set()
    preview_evidence = []
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            raise ProbeError(f"{step_name}: output #{index} 不是 object")
        prompt_id = str(image.get("prompt_id") or "").strip()
        key = _image_ref_key(image.get("image_ref"))
        try:
            size_bytes = int(image.get("size_bytes") or 0)
        except (TypeError, ValueError):
            size_bytes = 0
        if not prompt_id or not key or size_bytes <= 0:
            raise ProbeError(f"{step_name}: output #{index} 缺少 prompt_id/image_ref/size")
        prompt_ids.add(prompt_id)
        output_keys.append(key)
        preview_evidence.append(
            _validate_preview(client, image_ref=image["image_ref"], expected_size=size_bytes)
        )
    if len(prompt_ids) != 1 or len(set(output_keys)) != len(output_keys):
        raise ProbeError(f"{step_name}: outputs prompt_id 不一致或 image_ref 重複")
    primary = result.get("image") if isinstance(result.get("image"), dict) else {}
    if _image_ref_key(primary.get("image_ref")) != output_keys[0]:
        raise ProbeError(f"{step_name}: primary image 與 images[0] 不一致")

    history_payload = client.get_json("/api/comfyui/history", allow_http_error=True)
    if history_payload.get("_http_status") != 200 or history_payload.get("ok") is not True:
        raise ProbeError(f"{step_name}: history inventory 讀取失敗")
    history_rows = history_payload.get("history") if isinstance(history_payload.get("history"), list) else []
    matches = [
        row for row in history_rows
        if isinstance(row, dict) and str(row.get("id") or "") == str(history_id)
    ]
    if len(matches) != 1:
        raise ProbeError(f"{step_name}: history_id={history_id} 精確匹配數不是 1")
    history = matches[0]
    payload = history.get("payload") if isinstance(history.get("payload"), dict) else {}
    history_result = history.get("result") if isinstance(history.get("result"), dict) else {}
    if str(history.get("generation_mode") or "").strip().lower() != str(expected_mode).strip().lower():
        raise ProbeError(f"{step_name}: history generation_mode correlation mismatch")
    if str(payload.get("model") or "").strip() != str(expected_model).strip():
        raise ProbeError(f"{step_name}: history model correlation mismatch")
    if str(payload.get("prompt") or "") != str(expected_prompt):
        raise ProbeError(f"{step_name}: history prompt correlation mismatch")
    prompt_id = next(iter(prompt_ids))
    if str(history_result.get("prompt_id") or "").strip() != prompt_id:
        raise ProbeError(f"{step_name}: history prompt_id correlation mismatch")
    history_images = history_result.get("images") if isinstance(history_result.get("images"), list) else []
    history_keys = [_image_ref_key(item.get("image_ref")) for item in history_images if isinstance(item, dict)]
    if history_keys != output_keys:
        raise ProbeError(f"{step_name}: history output refs 與 job outputs 不一致")

    input_assets = history.get("input_assets") if isinstance(history.get("input_assets"), dict) else {}
    expected_inputs = expected_inputs if isinstance(expected_inputs, dict) else {}
    actual_inputs = {}
    for field in ("source_image_ref", "mask_image_ref", "control_image_ref"):
        expected_filename = str(expected_inputs.get(field) or "").strip()
        actual_ref = input_assets.get(field)
        if expected_filename:
            key = _image_ref_key(actual_ref)
            unique_marker = Path(expected_filename).stem
            if not key or unique_marker not in Path(key[0]).stem or key[2] != "input":
                raise ProbeError(f"{step_name}: {field} unique upload correlation mismatch")
            actual_inputs[field] = actual_ref
        elif actual_ref:
            raise ProbeError(f"{step_name}: history 出現未預期的 {field}")
    if expected_upscale_model and str(payload.get("upscale_model") or "") != str(expected_upscale_model):
        raise ProbeError(f"{step_name}: upscale model correlation mismatch")
    if expected_controlnet:
        history_control = history.get("controlnet") if isinstance(history.get("controlnet"), dict) else {}
        for key in ("type", "model_name", "preprocessor"):
            if str(history_control.get(key) or "") != str(expected_controlnet.get(key) or ""):
                raise ProbeError(f"{step_name}: ControlNet {key} correlation mismatch")
    return {
        "job_id": str(job_id),
        "terminal_status": "completed",
        "history_id": history_id,
        "prompt_id": prompt_id,
        "output_count": len(output_keys),
        "outputs": preview_evidence,
        "input_assets": actual_inputs,
        "correlation": {
            "job_to_result": True,
            "result_to_history": True,
            "prompt_id": True,
            "output_refs": True,
            "input_refs": True,
            "model": True,
            "mode": True,
            "prompt": True,
        },
    }


def execute_generation_step(
    client,
    *,
    step_name,
    submit,
    timeout_seconds,
    cancel_grace_seconds,
    validation_kwargs,
):
    job_id = ""
    submitted = {}
    try:
        submitted = submit()
        job = _job(submitted)
        job_id = str(job.get("job_id") or "").strip()
        if (
            submitted.get("_http_status") != 200
            or submitted.get("ok") is not True
            or submitted.get("async") is not True
            or not job_id
        ):
            receipt = bounded_interrupt(
                client,
                job_id=job_id,
                reason=f"{step_name}_invalid_async_contract",
                grace_seconds=cancel_grace_seconds,
            )
            raise JobExecutionError(
                f"{step_name}: generation 必須回傳 HTTP 200 / ok=true / async=true / job_id；禁止同步 ok/no-job PASS",
                abort_receipt=receipt,
                payload=submitted,
            )
        terminal = wait_for_job(
            client,
            job_id,
            timeout_seconds=timeout_seconds,
            cancel_grace_seconds=cancel_grace_seconds,
        )
        terminal_state = str(_job(terminal).get("status") or "").strip().lower()
        if terminal_state != "completed":
            receipt = bounded_interrupt(
                client,
                job_id=job_id,
                reason=f"{step_name}_terminal_{terminal_state or 'unknown'}",
                grace_seconds=cancel_grace_seconds,
            )
            raise JobExecutionError(
                f"{step_name}: job reached non-success terminal state {terminal_state!r}",
                abort_receipt=receipt,
                payload=terminal,
            )
        evidence = validate_terminal_generation(
            client,
            terminal,
            job_id=job_id,
            step_name=step_name,
            **validation_kwargs,
        )
        return _probe_result(
            step_name,
            ok=True,
            status="pass",
            payload={
                "http_status": submitted.get("_http_status"),
                "job_status": "completed",
                "evidence": evidence,
                "abort_receipt": {"attempted": False, "reason": "terminal_completed", "ok": True},
            },
        ), evidence
    except JobExecutionError as exc:
        return _probe_result(
            step_name,
            ok=False,
            status="fail",
            detail=str(exc),
            payload={
                "http_status": submitted.get("_http_status"),
                "job_id": job_id,
                "abort_receipt": exc.abort_receipt,
                "last_job": exc.payload,
            },
        ), None
    except Exception as exc:
        terminal_status = str(_job(locals().get("terminal") or {}).get("status") or "").strip().lower()
        receipt = {
            "attempted": False,
            "reason": "terminal_job_observed_before_validation_failure",
            "terminal_status": terminal_status,
            "ok": terminal_status in TERMINAL_JOB_STATES,
        }
        if job_id and terminal_status not in TERMINAL_JOB_STATES:
            receipt = bounded_interrupt(
                client,
                job_id=job_id,
                reason=f"{step_name}_exception",
                grace_seconds=cancel_grace_seconds,
            )
        return _probe_result(
            step_name,
            ok=False,
            status="fail",
            detail=str(exc),
            payload={
                "http_status": submitted.get("_http_status"),
                "job_id": job_id,
                "abort_receipt": receipt,
            },
        ), None


def _discard_absence_proof_valid(discard):
    discard = discard if isinstance(discard, dict) else {}
    binding = discard.get("local_binding") if isinstance(discard.get("local_binding"), dict) else {}
    verification = str(discard.get("verification") or "")
    if verification == "local_lstat_absent":
        listener_pid = binding.get("listener_pid")
        listener_inode = str(binding.get("listener_inode") or "")
        listener_cwd = str(binding.get("listener_cwd") or "")
        project_dir = str(binding.get("project_dir") or "")
        listeners = binding.get("listeners") if isinstance(binding.get("listeners"), list) else []
        proof_ok = bool(
            binding.get("binding_verified") is True
            and isinstance(listener_pid, int)
            and listener_pid > 0
            and listener_inode.isdigit()
            and listener_cwd
            and listener_cwd == project_dir
            and any(
                isinstance(item, dict)
                and item.get("pid") == listener_pid
                and str(item.get("inode") or "") == listener_inode
                and str(item.get("cwd") or "") == listener_cwd
                and item.get("cwd_matches_project") is True
                for item in listeners
            )
        )
    elif verification == "http_404":
        proof_ok = binding.get("binding_verified") is False
    else:
        proof_ok = False
    return bool(
        discard.get("absence_verified") is True
        and (discard.get("file_deleted") is True or discard.get("file_missing") is True)
        and discard.get("remote_preview_only") is False
        and proof_ok
    )


def cleanup_probe_inputs(client, tracked_inputs):
    rows = []
    seen = set()
    for item in tracked_inputs or []:
        if not isinstance(item, dict):
            continue
        image_ref = item.get("image_ref") if isinstance(item.get("image_ref"), dict) else {}
        key = _image_ref_key(image_ref)
        if not key or key in seen:
            continue
        seen.add(key)
        row = {
            "step": item.get("step") or "",
            "field": item.get("field") or "",
            "image_ref": image_ref,
            "correlated": item.get("correlated") is True,
            "exact": False,
            "immutable_residual": False,
        }
        try:
            response = client.post_json(
                "/api/comfyui/discard",
                {"image_ref": image_ref, "prompt_id": ""},
                allow_http_error=True,
            )
            discard = response.get("discard") if isinstance(response.get("discard"), dict) else {}
            row["response"] = response
            row["immutable_residual"] = bool(
                discard.get("remote_preview_only")
                or response.get("warning") == "source_file_not_deleted"
                or (
                    response.get("ok") is True
                    and not discard.get("file_deleted")
                    and not discard.get("file_missing")
                )
            )
            row["exact"] = bool(
                row["correlated"]
                and response.get("_http_status") == 200
                and response.get("ok") is True
                and _discard_absence_proof_valid(discard)
                and not row["immutable_residual"]
            )
        except Exception as exc:
            row["error"] = str(exc)
        rows.append(row)
    immutable = [row for row in rows if row.get("immutable_residual")]
    uncertain = [row for row in rows if not row.get("correlated")]
    failures = [row for row in rows if row.get("exact") is not True]
    return {
        "attempted_count": len(rows),
        "exact_deleted_or_missing_count": len(rows) - len(failures),
        "exact": not failures,
        "immutable_residuals": immutable,
        "uncertain_uploads": uncertain,
        "failures": failures,
        "rows": rows,
    }


def _probe_run_id(value=""):
    raw = str(value or "").strip() or f"{int(time.time())}-{secrets.token_hex(6)}"
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "-" for char in raw)
    return cleaned[:64] or secrets.token_hex(8)


def _unique_probe_file(run_id, step, field, data):
    filename = f"hackme-feature-{run_id}-{step}-{field}.png"
    return {
        "field": field,
        "filename": filename,
        "content_type": "image/png",
        "data": data,
    }


def run_probe(args):
    client = WebClient(
        args.base_url,
        insecure=args.insecure,
        request_timeout=getattr(args, "http_timeout", 10),
    )
    results = []
    summary = {"base_url": args.base_url, "started_at": _now()}
    client.login(args.username, args.password)

    status = client.get_json("/api/comfyui/status", allow_http_error=True)
    results.append(
        _probe_result(
            "status",
            ok=status.get("_http_status") == 200 and status.get("available") is True,
            status="pass" if status.get("_http_status") == 200 and status.get("available") is True else "fail",
            detail=status.get("msg") or "",
            payload={
                "http_status": status.get("_http_status"),
                "available": status.get("available"),
                "comfyui_url": status.get("comfyui_url"),
            },
        )
    )
    if status.get("_http_status") != 200 or status.get("available") is not True:
        return {"ok": False, "summary": summary, "results": results}

    models = client.get_json("/api/comfyui/models", allow_http_error=True)
    model_name = ""
    upscale_model = ""
    controlnet_selection = {}
    model_selection_error = ""
    try:
        model_name = select_checkpoint_model(models, getattr(args, "model", ""))
        upscale_model = select_exact_inventory_value(
            models,
            getattr(args, "upscale_model", ""),
            inventory_key="upscale_models",
            label="upscale model",
        )
        controlnet_selection = select_controlnet_dependencies(
            models,
            controlnet_type=getattr(args, "controlnet_type", ""),
            model_name=getattr(args, "controlnet_model", ""),
            preprocessor=getattr(args, "controlnet_preprocessor", ""),
        )
    except ProbeError as exc:
        model_selection_error = str(exc)
    controlnet_models = models.get("controlnet_models") or []
    models_ok = bool(model_name and upscale_model and controlnet_selection)
    results.append(
        _probe_result(
            "models",
            ok=models_ok,
            status="pass" if models_ok else "fail",
            detail=model_selection_error,
            payload={
                "http_status": models.get("_http_status"),
                "inventory_ok": models.get("ok"),
                "models": models.get("models") or [],
                "upscale_models": models.get("upscale_models") or [],
                "controlnet_models": controlnet_models,
                "selection_rule": "explicit_exact_inventory_match_no_fallback",
                "requested_model": str(getattr(args, "model", "") or "").strip(),
                "selected_model": model_name,
                "requested_upscale_model": str(getattr(args, "upscale_model", "") or "").strip(),
                "selected_upscale_model": upscale_model,
                "controlnet_selection": controlnet_selection,
            },
        )
    )
    if not models_ok:
        return {"ok": False, "summary": summary, "results": results}
    summary["checkpoint_model"] = model_name
    summary["upscale_model"] = upscale_model
    summary["controlnet"] = controlnet_selection

    run_id = _probe_run_id(getattr(args, "probe_run_id", ""))
    summary["probe_run_id"] = run_id
    tracked_inputs = []
    created_history_ids = []
    successful_history_id = 0
    failed = False

    def make_files(step, specs):
        files = []
        for field, data in specs:
            file_item = _unique_probe_file(run_id, step, field, data)
            files.append(file_item)
            tracked_inputs.append({
                "step": step,
                "field": field,
                "correlated": False,
                "image_ref": {
                    "filename": file_item["filename"],
                    "subfolder": "",
                    "type": "input",
                },
            })
        return files

    def expected_inputs(files):
        field_map = {
            "source_image": "source_image_ref",
            "mask_image": "mask_image_ref",
            "control_image": "control_image_ref",
        }
        return {field_map[item["field"]]: item["filename"] for item in files}

    def mark_correlated_inputs(step, evidence):
        actual = evidence.get("input_assets") if isinstance(evidence, dict) else {}
        reverse = {
            "source_image_ref": "source_image",
            "mask_image_ref": "mask_image",
            "control_image_ref": "control_image",
        }
        for history_field, image_ref in (actual or {}).items():
            request_field = reverse.get(history_field)
            for item in tracked_inputs:
                if item.get("step") == step and item.get("field") == request_field:
                    item["image_ref"] = image_ref
                    item["correlated"] = True

    try:
        baseline = client.get_json("/api/comfyui/history", allow_http_error=True)
        baseline_rows = baseline.get("history") if isinstance(baseline.get("history"), list) else []
        baseline_ok = baseline.get("_http_status") == 200 and baseline.get("ok") is True
        baseline_all_ids = {
            str(item.get("id"))
            for item in baseline_rows
            if isinstance(item, dict) and item.get("id") not in (None, "")
        }
        baseline_history_ids = {
            int(item["id"])
            for item in baseline_rows
            if isinstance(item, dict) and str(item.get("id") or "").isdigit()
        }
        results.append(
            _probe_result(
                "history_baseline",
                ok=baseline_ok,
                status="pass" if baseline_ok else "fail",
                detail=f"numeric_history_count={len(baseline_history_ids)}",
                payload={
                    "http_status": baseline.get("_http_status"),
                    "history_ids": sorted(baseline_all_ids),
                    "numeric_history_ids": sorted(baseline_history_ids),
                },
            )
        )
        failed = not baseline_ok

        def run_case(
            step_name,
            payload,
            *,
            files=None,
            expected_mode=None,
            expected_controlnet=None,
            expected_upscale_model="",
            submit_path="/api/comfyui/generate",
        ):
            nonlocal failed, successful_history_id
            if failed:
                return None
            payload = dict(payload)
            payload["timeout_seconds"] = int(args.timeout)
            files = list(files or [])
            if files:
                submit = lambda: client.post_multipart(
                    submit_path,
                    fields=payload,
                    files=files,
                    allow_http_error=True,
                )
            else:
                submit = lambda: client.post_json(
                    submit_path,
                    payload,
                    allow_http_error=True,
                )
            row, evidence = execute_generation_step(
                client,
                step_name=step_name,
                submit=submit,
                timeout_seconds=args.timeout,
                cancel_grace_seconds=getattr(args, "cancel_grace", 30),
                validation_kwargs={
                    "expected_mode": expected_mode or payload.get("generation_mode") or "txt2img",
                    "expected_model": model_name,
                    "expected_prompt": payload.get("prompt") or "",
                    "baseline_history_ids": baseline_history_ids | set(created_history_ids),
                    "expected_inputs": expected_inputs(files),
                    "expected_controlnet": expected_controlnet,
                    "expected_upscale_model": expected_upscale_model,
                },
            )
            results.append(row)
            if not evidence:
                failed = True
                return None
            history_id = int(evidence["history_id"])
            created_history_ids.append(history_id)
            mark_correlated_inputs(step_name, evidence)
            if step_name == "txt2img":
                successful_history_id = history_id
            return evidence

        txt_payload = minimal_generate_json(model_name, mode="txt2img", probe_run_id=run_id)
        run_case("txt2img", txt_payload)

        img_files = make_files("img2img", [("source_image", SOURCE_PNG)]) if not failed else []
        img_payload = minimal_generate_json(model_name, mode="img2img", probe_run_id=run_id)
        run_case("img2img", img_payload, files=img_files)

        inpaint_files = make_files(
            "inpaint",
            [("source_image", SOURCE_PNG), ("mask_image", MASK_PNG)],
        ) if not failed else []
        inpaint_payload = minimal_generate_json(model_name, mode="inpaint", probe_run_id=run_id)
        run_case("inpaint", inpaint_payload, files=inpaint_files)

        outpaint_files = make_files("outpaint", [("source_image", SOURCE_PNG)]) if not failed else []
        outpaint_payload = minimal_generate_json(model_name, mode="outpaint", probe_run_id=run_id)
        outpaint_payload.update({
            "outpaint_left": 32,
            "outpaint_top": 16,
            "outpaint_right": 16,
            "outpaint_bottom": 16,
            "outpaint_feathering": 24,
        })
        run_case("outpaint", outpaint_payload, files=outpaint_files)

        upscale_files = make_files("upscale", [("source_image", SOURCE_PNG)]) if not failed else []
        upscale_payload = minimal_generate_json(
            model_name,
            mode="upscale",
            upscale_model=upscale_model,
            probe_run_id=run_id,
        )
        run_case(
            "upscale",
            upscale_payload,
            files=upscale_files,
            expected_upscale_model=upscale_model,
        )

        control_files = make_files(
            "controlnet",
            [("source_image", SOURCE_PNG), ("control_image", CONTROL_PNG)],
        ) if not failed else []
        controlnet_payload = minimal_generate_json(model_name, mode="img2img", probe_run_id=run_id)
        controlnet_payload.update({
            "controlnet_enabled": True,
            "controlnet_type": controlnet_selection["type"],
            "controlnet_model": controlnet_selection["model_name"],
            "controlnet_preprocessor": controlnet_selection["preprocessor"],
            "control_strength": 0.8,
            "control_start": 0.0,
            "control_end": 1.0,
        })
        run_case(
            "controlnet",
            controlnet_payload,
            files=control_files,
            expected_mode="img2img",
            expected_controlnet=controlnet_selection,
        )

        if not failed:
            history = client.get_json("/api/comfyui/history", allow_http_error=True)
            history_items = history.get("history") if isinstance(history.get("history"), list) else []
            current_all_ids = [
                str(item.get("id"))
                for item in history_items
                if isinstance(item, dict) and item.get("id") not in (None, "")
            ]
            history_delta = set(current_all_ids) - baseline_all_ids
            expected_delta = {str(item) for item in created_history_ids}
            history_ok = bool(
                history.get("_http_status") == 200
                and history.get("ok") is True
                and len(current_all_ids) == len(set(current_all_ids))
                and history_delta == expected_delta
            )
            results.append(
                _probe_result(
                    "history_list",
                    ok=history_ok,
                    status="pass" if history_ok else "fail",
                    detail=f"history_count={len(history_items)}, exact_created={len(created_history_ids)}",
                    payload={
                        "http_status": history.get("_http_status"),
                        "history_count": len(history_items),
                        "created_history_ids": list(created_history_ids),
                        "baseline_delta_ids": sorted(history_delta),
                        "exact_baseline_delta": history_delta == expected_delta,
                        "duplicate_history_ids": len(current_all_ids) != len(set(current_all_ids)),
                    },
                )
            )
            failed = not history_ok

        if not failed and successful_history_id > 0:
            rerun_payload = dict(txt_payload)
            rerun_payload["timeout_seconds"] = int(args.timeout)
            run_case(
                "history_rerun",
                rerun_payload,
                submit_path=f"/api/comfyui/history/{successful_history_id}/rerun",
            )
            if results[-1].get("name") == "history_rerun":
                results[-1]["payload"]["rerun_source_history_id"] = successful_history_id
                results[-1]["payload"]["exact_source_correlation"] = True
        elif not failed:
            results.append(
                _probe_result(
                    "history_rerun",
                    ok=False,
                    status="fail",
                    detail="txt2img 沒有可精確關聯的 history_id；禁止重跑任意 history",
                    payload={"rerun_source_history_id": 0, "exact_source_correlation": False},
                )
            )
            failed = True

        if not failed:
            final_history = client.get_json("/api/comfyui/history", allow_http_error=True)
            final_rows = final_history.get("history") if isinstance(final_history.get("history"), list) else []
            final_ids = [
                str(item.get("id"))
                for item in final_rows
                if isinstance(item, dict) and item.get("id") not in (None, "")
            ]
            final_delta = set(final_ids) - baseline_all_ids
            expected_final_delta = {str(item) for item in created_history_ids}
            final_delta_ok = bool(
                final_history.get("_http_status") == 200
                and final_history.get("ok") is True
                and len(final_ids) == len(set(final_ids))
                and final_delta == expected_final_delta
            )
            results.append(
                _probe_result(
                    "history_final_delta",
                    ok=final_delta_ok,
                    status="pass" if final_delta_ok else "fail",
                    detail=(
                        f"created={len(created_history_ids)}, baseline_delta={len(final_delta)}"
                    ),
                    payload={
                        "http_status": final_history.get("_http_status"),
                        "created_history_ids": list(created_history_ids),
                        "baseline_delta_ids": sorted(final_delta),
                        "exact_baseline_delta": final_delta == expected_final_delta,
                        "duplicate_history_ids": len(final_ids) != len(set(final_ids)),
                    },
                )
            )
            failed = not final_delta_ok
    except Exception as exc:
        results.append(_probe_result("probe_execution", ok=False, status="fail", detail=str(exc), payload={}))
        failed = True
    finally:
        cleanup = cleanup_probe_inputs(client, tracked_inputs)
        results.append(
            _probe_result(
                "input_cleanup",
                ok=cleanup["exact"],
                status="pass" if cleanup["exact"] else "fail",
                detail=(
                    "all unique uploaded inputs deleted or already missing"
                    if cleanup["exact"]
                    else "uploaded input cleanup is not exact; immutable or uncertain residuals remain"
                ),
                payload=cleanup,
            )
        )
        summary["input_cleanup"] = cleanup

    summary["created_history_ids"] = list(created_history_ids)
    summary["finished_at"] = _now()
    summary["overall_ok"] = bool(
        not failed
        and results
        and all(item.get("ok") is True and item.get("status") == "pass" for item in results)
    )
    return {"ok": summary["overall_ok"], "summary": summary, "results": results}


def parse_args():
    parser = argparse.ArgumentParser(description="Live probe hackme_web ComfyUI features against a running server.")
    parser.add_argument("--base-url", required=True, help="hackme_web base URL, e.g. https://127.0.0.1:5014")
    parser.add_argument("--username", default="root")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser, "--password")
    parser.add_argument("--timeout", type=int, default=180, help="Per async job wait timeout in seconds")
    parser.add_argument(
        "--model",
        "--checkpoint-model",
        dest="model",
        required=True,
        help="Exact checkpoint name from /api/comfyui/models; no automatic or fuzzy fallback is allowed",
    )
    parser.add_argument(
        "--upscale-model",
        required=True,
        help="Exact upscale model from /api/comfyui/models; no first-item fallback is allowed",
    )
    parser.add_argument("--controlnet-type", required=True, help="Exact available ControlNet type to probe")
    parser.add_argument(
        "--controlnet-model",
        required=True,
        help="Exact ControlNet model from the selected type's matching_models inventory",
    )
    parser.add_argument(
        "--controlnet-preprocessor",
        required=True,
        help="Exact ControlNet preprocessor from the selected type's available_preprocessors inventory",
    )
    parser.add_argument("--probe-run-id", default="", help="Optional unique campaign/run correlation id")
    parser.add_argument(
        "--cancel-grace",
        type=int,
        default=30,
        help="Seconds to verify terminal state after a bounded interrupt",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=10,
        help="Per HTTP operation timeout so abort and evidence collection cannot hang indefinitely",
    )
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification for local/self-signed servers")
    parser.add_argument("--json-out", default="", help="Optional path to save the JSON report")
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        report = run_probe(args)
    except Exception as exc:
        report = {
            "ok": False,
            "summary": {"base_url": args.base_url, "started_at": _now(), "finished_at": _now(), "overall_ok": False},
            "results": [_probe_result("probe", ok=False, status="fail", detail=str(exc), payload={})],
        }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    print(text)
    raise SystemExit(0 if report.get("ok") else 1)


if __name__ == "__main__":
    main()
