#!/usr/bin/env python3
import argparse
import base64
import http.client
import http.cookiejar
import json
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path


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


class WebClient:
    def __init__(self, base_url, *, insecure=False, user_agent="hackme_web-comfyui-probe/1.0"):
        self.base_url = str(base_url).rstrip("/")
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
            with self.opener.open(req) as resp:
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


def wait_for_job(client, job_id, *, timeout_seconds=180):
    deadline = time.time() + max(5, int(timeout_seconds))
    last = None
    while time.time() < deadline:
        payload = client.get_json(f"/api/comfyui/jobs/{urllib.parse.quote(str(job_id))}", allow_http_error=True)
        last = payload
        job = payload.get("job") or {}
        status = str(job.get("status") or "").strip().lower()
        if status in {"completed", "error"}:
            return payload
        time.sleep(0.8)
    raise ProbeError(f"等待 ComfyUI job {job_id} 完成逾時；最後狀態：{json.dumps(last or {}, ensure_ascii=False)[:400]}")


def minimal_generate_json(model_name, *, mode="txt2img", upscale_model=""):
    payload = {
        "generation_mode": mode,
        "model": model_name,
        "prompt": f"hackme_web {mode} probe",
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
    }
    if upscale_model:
        payload["upscale_model"] = upscale_model
    return payload


def run_probe(args):
    client = WebClient(args.base_url, insecure=args.insecure)
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
    model_name = (models.get("models") or [None])[0]
    upscale_model = (models.get("upscale_models") or [None])[0]
    controlnet_models = models.get("controlnet_models") or []
    history_supported = True
    results.append(
        _probe_result(
            "models",
            ok=models.get("_http_status") == 200 and bool(model_name),
            status="pass" if models.get("_http_status") == 200 and bool(model_name) else "fail",
            detail="" if model_name else "找不到任何 checkpoint model",
            payload={
                "http_status": models.get("_http_status"),
                "models": models.get("models") or [],
                "upscale_models": models.get("upscale_models") or [],
                "controlnet_models": controlnet_models,
            },
        )
    )
    if not model_name:
        return {"ok": False, "summary": summary, "results": results}

    successful_history_id = None

    txt2img = client.post_json("/api/comfyui/generate", minimal_generate_json(model_name), allow_http_error=True)
    if txt2img.get("ok") and txt2img.get("async") and _job(txt2img).get("job_id"):
        txt2img = wait_for_job(client, _job(txt2img)["job_id"], timeout_seconds=args.timeout)
    txt_ok = _job(txt2img).get("status") == "completed" or (txt2img.get("ok") is True and not _job(txt2img))
    if txt_ok:
        successful_history_id = txt2img.get("history_id") or _job_result(txt2img).get("history_id")
    results.append(
        _probe_result(
            "txt2img",
            ok=txt_ok,
            status="pass" if txt_ok else "fail",
            detail=_job_error(txt2img),
            payload={"http_status": txt2img.get("_http_status"), "job_status": _job(txt2img).get("status")},
        )
    )

    common_files = [
        {"field": "source_image", "filename": "source.png", "content_type": "image/png", "data": SOURCE_PNG},
    ]
    img2img = client.post_multipart(
        "/api/comfyui/generate",
        fields=minimal_generate_json(model_name, mode="img2img"),
        files=common_files,
        allow_http_error=True,
    )
    if img2img.get("ok") and img2img.get("async") and _job(img2img).get("job_id"):
        img2img = wait_for_job(client, _job(img2img)["job_id"], timeout_seconds=args.timeout)
    img_ok = _job(img2img).get("status") == "completed" or (img2img.get("ok") is True and not _job(img2img))
    results.append(
        _probe_result(
            "img2img",
            ok=img_ok,
            status="pass" if img_ok else "fail",
            detail=_job_error(img2img),
            payload={"http_status": img2img.get("_http_status"), "job_status": _job(img2img).get("status")},
        )
    )

    inpaint = client.post_multipart(
        "/api/comfyui/generate",
        fields=minimal_generate_json(model_name, mode="inpaint"),
        files=common_files + [
            {"field": "mask_image", "filename": "mask.png", "content_type": "image/png", "data": MASK_PNG},
        ],
        allow_http_error=True,
    )
    if inpaint.get("ok") and inpaint.get("async") and _job(inpaint).get("job_id"):
        inpaint = wait_for_job(client, _job(inpaint)["job_id"], timeout_seconds=args.timeout)
    inpaint_ok = _job(inpaint).get("status") == "completed" or (inpaint.get("ok") is True and not _job(inpaint))
    results.append(
        _probe_result(
            "inpaint",
            ok=inpaint_ok,
            status="pass" if inpaint_ok else "fail",
            detail=_job_error(inpaint),
            payload={"http_status": inpaint.get("_http_status"), "job_status": _job(inpaint).get("status")},
        )
    )

    outpaint_fields = minimal_generate_json(model_name, mode="outpaint")
    outpaint_fields.update({
        "outpaint_left": 32,
        "outpaint_top": 16,
        "outpaint_right": 16,
        "outpaint_bottom": 16,
        "outpaint_feathering": 24,
    })
    outpaint = client.post_multipart(
        "/api/comfyui/generate",
        fields=outpaint_fields,
        files=common_files,
        allow_http_error=True,
    )
    if outpaint.get("ok") and outpaint.get("async") and _job(outpaint).get("job_id"):
        outpaint = wait_for_job(client, _job(outpaint)["job_id"], timeout_seconds=args.timeout)
    outpaint_ok = _job(outpaint).get("status") == "completed" or (outpaint.get("ok") is True and not _job(outpaint))
    results.append(
        _probe_result(
            "outpaint",
            ok=outpaint_ok,
            status="pass" if outpaint_ok else "fail",
            detail=_job_error(outpaint),
            payload={"http_status": outpaint.get("_http_status"), "job_status": _job(outpaint).get("status")},
        )
    )

    if upscale_model:
        upscale = client.post_multipart(
            "/api/comfyui/generate",
            fields=minimal_generate_json(model_name, mode="upscale", upscale_model=upscale_model),
            files=common_files,
            allow_http_error=True,
        )
        if upscale.get("ok") and upscale.get("async") and _job(upscale).get("job_id"):
            upscale = wait_for_job(client, _job(upscale)["job_id"], timeout_seconds=args.timeout)
        upscale_ok = _job(upscale).get("status") == "completed" or (upscale.get("ok") is True and not _job(upscale))
        results.append(
            _probe_result(
                "upscale",
                ok=upscale_ok,
                status="pass" if upscale_ok else "fail",
                detail=_job_error(upscale),
                payload={"http_status": upscale.get("_http_status"), "job_status": _job(upscale).get("status"), "upscale_model": upscale_model},
            )
        )
    else:
        results.append(
            _probe_result(
                "upscale",
                ok=False,
                status="expected_unavailable",
                detail="ComfyUI 未回傳任何 upscale model；請先安裝 scale model。",
                payload={},
            )
        )

    controlnet_fields = minimal_generate_json(model_name, mode="img2img")
    controlnet_fields.update({
        "controlnet_enabled": True,
        "controlnet_type": args.controlnet_type,
        "control_strength": 0.8,
        "control_start": 0.0,
        "control_end": 1.0,
    })
    controlnet = client.post_multipart(
        "/api/comfyui/generate",
        fields=controlnet_fields,
        files=common_files + [
            {"field": "control_image", "filename": "control.png", "content_type": "image/png", "data": CONTROL_PNG},
        ],
        allow_http_error=True,
    )
    control_msg = str(controlnet.get("msg") or "")
    control_available = bool(controlnet_models) and models.get("controlnet_types", {}).get(args.controlnet_type, {}).get("available") is True
    if control_available and controlnet.get("ok") and controlnet.get("async") and _job(controlnet).get("job_id"):
        controlnet = wait_for_job(client, _job(controlnet)["job_id"], timeout_seconds=args.timeout)
        control_ok = _job(controlnet).get("status") == "completed" or (controlnet.get("ok") is True and not _job(controlnet))
        results.append(
            _probe_result(
                "controlnet",
                ok=control_ok,
                status="pass" if control_ok else "fail",
                detail=_job_error(controlnet),
                payload={"http_status": controlnet.get("_http_status"), "job_status": _job(controlnet).get("status"), "controlnet_type": args.controlnet_type},
            )
        )
    else:
        expected = ("缺少對應" in control_msg) or ("nodes" in control_msg.lower()) or ("models" in control_msg.lower())
        results.append(
            _probe_result(
                "controlnet",
                ok=expected,
                status="expected_unavailable" if expected else "fail",
                detail=control_msg or "ControlNet 目前不可用",
                payload={"http_status": controlnet.get("_http_status"), "controlnet_type": args.controlnet_type},
            )
        )

    history = client.get_json("/api/comfyui/history", allow_http_error=True)
    history_items = history.get("history") or []
    results.append(
        _probe_result(
            "history_list",
            ok=history.get("_http_status") == 200,
            status="pass" if history.get("_http_status") == 200 else "fail",
            detail=f"history_count={len(history_items)}",
            payload={"http_status": history.get("_http_status"), "history_count": len(history_items)},
        )
    )
    rerun_target = history_items[0]["id"] if history_items else successful_history_id
    if rerun_target:
        rerun = client.post_json(f"/api/comfyui/history/{int(rerun_target)}/rerun", {}, allow_http_error=True)
        if rerun.get("ok") and _job(rerun).get("job_id"):
            rerun = wait_for_job(client, _job(rerun)["job_id"], timeout_seconds=args.timeout)
        rerun_ok = _job(rerun).get("status") == "completed" or (rerun.get("ok") is True and not _job(rerun))
        results.append(
            _probe_result(
                "history_rerun",
                ok=rerun_ok,
                status="pass" if rerun_ok else "fail",
                detail=_job_error(rerun),
                payload={"http_status": rerun.get("_http_status"), "job_status": _job(rerun).get("status"), "history_id": rerun_target},
            )
        )
    else:
        results.append(_probe_result("history_rerun", ok=False, status="skip", detail="沒有可重跑的歷史紀錄", payload={}))

    summary["finished_at"] = _now()
    summary["overall_ok"] = all(item["status"] in {"pass", "expected_unavailable", "skip"} for item in results)
    return {"ok": summary["overall_ok"], "summary": summary, "results": results}


def parse_args():
    parser = argparse.ArgumentParser(description="Live probe hackme_web ComfyUI features against a running server.")
    parser.add_argument("--base-url", required=True, help="hackme_web base URL, e.g. https://127.0.0.1:5014")
    parser.add_argument("--username", default="root")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser, "--password")
    parser.add_argument("--timeout", type=int, default=180, help="Per async job wait timeout in seconds")
    parser.add_argument("--controlnet-type", default="canny", help="ControlNet type to probe when available")
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
