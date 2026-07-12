#!/usr/bin/env python3
"""Run every official ComfyUI template through the real frontend.

The script deliberately uses the browser page's JavaScript helpers to collect
template defaults, media remaps, multi-compare settings, and breakpoint specs.
That keeps the test close to what a user actually triggers from the UI while
still giving QA structured artifacts for every template.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageStat
except Exception:  # pragma: no cover - optional QA nicety
    Image = None
    ImageDraw = None
    ImageStat = None

try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # pragma: no cover - environment failure
    raise SystemExit(f"Playwright is not available: {exc}") from exc


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from services.comfyui.template.seeding import SYSTEM_WORKFLOW_IDS
except Exception:
    SYSTEM_WORKFLOW_IDS = ()


@dataclass
class ImageAnalysis:
    width: int = 0
    height: int = 0
    mean: float = 0.0
    stddev: float = 0.0
    flags: tuple[str, ...] = ()


def now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def safe_slug(value: str, fallback: str = "item") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "")).strip("._")
    return cleaned[:120] or fallback


def decode_data_url(data_url: str) -> tuple[str, bytes]:
    if not data_url or "," not in data_url:
        return "application/octet-stream", b""
    header, payload = data_url.split(",", 1)
    mime = "application/octet-stream"
    if header.startswith("data:"):
        mime = header[5:].split(";", 1)[0] or mime
    return mime, base64.b64decode(payload)


def image_ext_for_mime(mime: str) -> str:
    if mime == "image/jpeg":
        return ".jpg"
    if mime == "image/webp":
        return ".webp"
    return ".png"


def media_ext_for_mime(mime: str) -> str:
    mapping = {
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "image/png": ".png",
        "image/jpeg": ".jpg",
    }
    return mapping.get(mime, ".bin")


def analyze_image(path: Path) -> ImageAnalysis:
    if Image is None or ImageStat is None:
        return ImageAnalysis()
    try:
        with Image.open(path) as img:
            rgb = img.convert("RGB")
            stat = ImageStat.Stat(rgb)
            mean = sum(stat.mean) / 3.0
            stddev = sum(stat.stddev) / 3.0
            flags: list[str] = []
            if rgb.width < 64 or rgb.height < 64:
                flags.append("tiny_output")
            if stddev < 4:
                flags.append("nearly_blank")
            if mean < 8:
                flags.append("almost_black")
            if mean > 247 and stddev < 8:
                flags.append("almost_white")
            return ImageAnalysis(rgb.width, rgb.height, round(mean, 2), round(stddev, 2), tuple(flags))
    except Exception as exc:
        return ImageAnalysis(flags=(f"image_decode_failed:{exc}",))


def save_data_url(data_url: str, path: Path) -> tuple[str, int]:
    mime, data = decode_data_url(data_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return mime, len(data)


def build_contact_sheets(image_items: list[dict[str, Any]], out_dir: Path) -> list[str]:
    if Image is None or ImageDraw is None or not image_items:
        return []
    sheets: list[str] = []
    tile_w, tile_h = 256, 314
    cols, rows = 4, 4
    per_sheet = cols * rows
    for sheet_index, start in enumerate(range(0, len(image_items), per_sheet), 1):
        chunk = image_items[start:start + per_sheet]
        sheet = Image.new("RGB", (cols * tile_w, rows * tile_h), "white")
        draw = ImageDraw.Draw(sheet)
        for index, item in enumerate(chunk):
            x = (index % cols) * tile_w
            y = (index // cols) * tile_h
            try:
                with Image.open(item["path"]) as src:
                    src = src.convert("RGB")
                    src.thumbnail((tile_w, tile_w), Image.Resampling.LANCZOS)
                    px = x + (tile_w - src.width) // 2
                    py = y + 4 + (tile_w - src.height) // 2
                    sheet.paste(src, (px, py))
            except Exception as exc:
                draw.text((x + 8, y + 80), f"load failed: {exc}", fill=(160, 0, 0))
            label = str(item.get("label") or item.get("bundle_id") or "output")
            prompt = str(item.get("prompt") or "").replace("\n", " ")
            text = f"{label[:44]}\n{prompt[:58]}"
            draw.rectangle((x, y + tile_w + 6, x + tile_w, y + tile_h), fill=(248, 248, 248))
            draw.text((x + 6, y + tile_w + 12), text, fill=(20, 20, 20))
        path = out_dir / f"contact_sheet_{sheet_index:02d}.jpg"
        sheet.save(path, quality=88)
        sheets.append(str(path))
    return sheets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Playwright full QA for official ComfyUI templates.")
    parser.add_argument("--base-url", default=os.environ.get("QA_BASE_URL", "https://127.0.0.1:5007"))
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--comfyui-api-url", default=os.environ.get("PLAYWRIGHT_COMFYUI_API_URL", "http://192.168.18.19:8188"))
    parser.add_argument("--out-dir", default=f"/tmp/hackme_comfyui_template_default_qa_{now_tag()}")
    parser.add_argument("--only", default="", help="Comma-separated system_bundle_id or preset id list.")
    parser.add_argument("--skip", default="", help="Comma-separated system_bundle_id or preset id list.")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--per-template-timeout", type=int, default=2100)
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    parser.add_argument("--headful", action="store_true")
    return parser.parse_args()


def browser_api(page, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    return page.evaluate(
        """async ({method, path, body}) => {
            await fetchCsrfToken({force: method !== "GET"});
            const options = {method, credentials: "same-origin", headers: {"X-CSRF-Token": getCsrfToken() || ""}};
            if (body !== null && body !== undefined) {
                options.headers["Content-Type"] = "application/json";
                options.body = JSON.stringify(body);
            }
            const res = await apiFetch(API + path, options);
            const json = await res.json().catch(() => ({}));
            return {status: res.status, ok: res.ok, body: json};
        }""",
        {"method": method, "path": path, "body": body},
    )


def request_failure_text(request) -> str:
    failure = None
    try:
        failure = request.failure
    except Exception as exc:
        return str(exc)[:300]
    if isinstance(failure, dict):
        return str(failure.get("errorText") or failure.get("message") or failure)[:300]
    return str(failure or "")[:300]


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    screenshots_dir = out_dir / "screenshots"
    images_dir = out_dir / "images"
    media_dir = out_dir / "media"
    out_dir.mkdir(parents=True, exist_ok=True)

    only = {item.strip() for item in args.only.split(",") if item.strip()}
    skip = {item.strip() for item in args.skip.split(",") if item.strip()}
    console_events: list[dict[str, str]] = []
    page_errors: list[str] = []
    network_errors: list[dict[str, str]] = []
    results: list[dict[str, Any]] = []
    contact_items: list[dict[str, Any]] = []

    order = {bundle_id: index for index, bundle_id in enumerate(SYSTEM_WORKFLOW_IDS)}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headful)
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
        page = context.new_page()
        page.set_default_timeout(120_000)
        page.on("console", lambda msg: console_events.append({"type": msg.type, "text": msg.text[:500]}))
        page.on("pageerror", lambda exc: page_errors.append(str(exc)[:1000]))
        page.on("requestfailed", lambda req: network_errors.append({
            "url": req.url,
            "method": req.method,
            "failure": request_failure_text(req),
        }))

        page.goto(args.base_url + "/", wait_until="domcontentloaded")
        page.wait_for_function("() => typeof fetchCsrfToken === 'function' && typeof apiFetch === 'function'")
        login = browser_api(page, "POST", "/login", {"username": "root", "password": args.root_password})
        if login["status"] != 200 or not login["body"].get("ok"):
            raise RuntimeError(f"login failed: {login}")
        page.goto(args.base_url + "/", wait_until="networkidle")
        page.wait_for_function("() => typeof fetchCsrfToken === 'function' && typeof apiFetch === 'function'")

        features = browser_api(page, "PUT", "/admin/features", {"feature_comfyui_enabled": True})
        if features["status"] != 200 or not features["body"].get("ok"):
            raise RuntimeError(f"feature enable failed: {features}")
        settings = browser_api(page, "PUT", "/admin/settings", {
            "comfyui_connection_mode": "remote",
            "comfyui_remote_api_url": args.comfyui_api_url,
        })
        if settings["status"] != 200 or not settings["body"].get("ok"):
            raise RuntimeError(f"ComfyUI settings failed: {settings}")
        connection = browser_api(page, "POST", "/root/comfyui/test-connection", {
            "connection_mode": "remote",
            "comfyui_connection_mode": "remote",
            "comfyui_remote_api_url": args.comfyui_api_url,
        })

        page.goto(args.base_url + "/", wait_until="networkidle")
        page.evaluate("""() => { if (typeof switchModuleTab === "function") switchModuleTab("comfyui"); }""")
        page.wait_for_selector("#comfyui-template-select", state="attached")
        presets_payload = page.evaluate(
            """async () => {
                const presets = await loadComfyuiWorkflowPresets({silentTemplateReload: false});
                return {
                    presets: presets.map((item) => ({
                        id: item.id,
                        title: item.title,
                        system_bundle_id: item.system_bundle_id || "",
                        purpose: item.purpose || "",
                        output_kinds: item.output_kinds || [],
                        default_params: item.default_params || {},
                        dependency_status: item.dependency_status || null,
                        is_official: !!item.is_official,
                    })),
                    selected: Number(comfyuiSelectedTemplatePresetId || 0),
                };
            }"""
        )
        presets = [item for item in presets_payload["presets"] if item.get("is_official")]
        presets.sort(key=lambda item: (order.get(item.get("system_bundle_id") or "", 9999), str(item.get("title") or "")))
        if only:
            presets = [
                item for item in presets
                if str(item.get("id")) in only or str(item.get("system_bundle_id") or "") in only
            ]
        if skip:
            presets = [
                item for item in presets
                if str(item.get("id")) not in skip and str(item.get("system_bundle_id") or "") not in skip
            ]
        if args.limit > 0:
            presets = presets[:args.limit]

        print(f"[qa] official presets to run: {len(presets)}", flush=True)

        for index, preset in enumerate(presets, 1):
            preset_id = int(preset["id"])
            bundle_id = str(preset.get("system_bundle_id") or f"preset_{preset_id}")
            slug = f"{index:02d}_{safe_slug(bundle_id)}"
            title = str(preset.get("title") or bundle_id)
            print(f"[qa] {index}/{len(presets)} start {bundle_id} - {title}", flush=True)
            started = time.time()
            item: dict[str, Any] = {
                "preset_id": preset_id,
                "bundle_id": bundle_id,
                "title": title,
                "purpose": preset.get("purpose") or "",
                "output_kinds": preset.get("output_kinds") or [],
                "status": "not_started",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "duration_seconds": None,
                "images": [],
                "media": [],
                "issues": [],
            }
            before_console = len(console_events)
            before_errors = len(page_errors)
            try:
                prepared = page.evaluate(
                    """async ({presetId}) => {
                        const select = document.getElementById("comfyui-template-select");
                        if (select) select.value = String(presetId);
                        await loadComfyuiSelectedTemplateDetail(presetId, {silent: true, applyDefaults: true});
                        if (comfyuiTemplateNeedsPromptSharingChoice(comfyuiSelectedTemplateDetail)) {
                            comfyuiTemplatePromptShareMode = "independent";
                            renderSelectedComfyuiTemplate();
                        }
                        const detail = comfyuiSelectedTemplateDetail;
                        const userInputs = collectComfyuiTemplateUserInputs(detail);
                        const imageAssignmentState = await ensureComfyuiTemplateImageAssignments(detail);
                        const multiCompareSpec = comfyuiTemplateIsMultiCompareCheckpoints(detail)
                            ? comfyuiMultiCompareRunSpec(detail)
                            : null;
                        const upscaleBreakpointSpec = comfyuiTemplateIsMultiMethodUpscale(detail)
                            ? comfyuiUpscaleBreakpointRunSpec(detail)
                            : null;
                        const sdxlRefinerSpec = comfyuiTemplateIsSdxlRefinerWorkflow(detail)
                            ? comfyuiSdxlRefinerRunSpec(detail)
                            : null;
                        const ggufWorkflowSpec = comfyuiTemplateIsGgufWorkflow(detail)
                            ? comfyuiGgufWorkflowRunSpec(detail)
                            : null;
                        const promptish = [];
                        Object.entries(userInputs || {}).forEach(([nodeId, inputs]) => {
                            Object.entries(inputs || {}).forEach(([name, value]) => {
                                const clean = String(value ?? "");
                                if (clean && /prompt|text|caption|negative/i.test(String(name))) {
                                    promptish.push({node_id: nodeId, input_name: name, value: clean.slice(0, 500)});
                                }
                            });
                        });
                        const wf = detail?.workflow_json || {};
                        const outputNodes = Object.entries(wf).filter(([, node]) => /Save|Preview/i.test(String(node?.class_type || ""))).map(([nodeId, node]) => ({
                            node_id: nodeId,
                            class_type: node?.class_type || "",
                            title: node?._meta?.title || "",
                        }));
                        return {
                            detail: {
                                id: detail?.id,
                                title: detail?.title || "",
                                system_bundle_id: detail?.system_bundle_id || "",
                                purpose: detail?.purpose || "",
                                output_kinds: detail?.output_kinds || [],
                                default_params: detail?.default_params || {},
                                dependency_status: detail?.dependency_status || null,
                                paid_api_nodes: detail?.paid_api_nodes || null,
                            },
                            userInputs,
                            promptish,
                            assignments: imageAssignmentState.assignments || {},
                            missingAssignments: imageAssignmentState.missing || [],
                            multiCompareSpec,
                            upscaleBreakpointSpec,
                            sdxlRefinerSpec,
                            ggufWorkflowSpec,
                            outputNodes,
                            templateMessage: document.getElementById("comfyui-message")?.textContent || "",
                        };
                    }""",
                    {"presetId": preset_id},
                )
                item["prepared"] = prepared
                if prepared.get("missingAssignments"):
                    item["status"] = "blocked_missing_media_assignment"
                    item["issues"].append("missing_media_assignment")
                    item["error"] = prepared["missingAssignments"]
                    print(f"[qa] {bundle_id} blocked by missing media assignment", flush=True)
                    continue

                run_result = page.evaluate(
                    """async ({presetId, prepared}) => {
                        await fetchCsrfToken({force: true});
                        const res = await apiFetch(API + `/comfyui/workflows/${encodeURIComponent(presetId)}/run`, {
                            method: "POST",
                            credentials: "same-origin",
                            headers: {
                                "Content-Type": "application/json",
                                "X-CSRF-Token": getCsrfToken() || "",
                            },
                            body: JSON.stringify({
                                confirm_paid_api_nodes: true,
                                user_inputs: prepared.userInputs || {},
                                image_field_assignments: prepared.assignments || {},
                                multi_compare: prepared.multiCompareSpec || undefined,
                                upscale_breakpoint: prepared.upscaleBreakpointSpec || undefined,
                                sdxl_refiner: prepared.sdxlRefinerSpec || undefined,
                                gguf_workflow: prepared.ggufWorkflowSpec || undefined,
                            }),
                        });
                        const json = await res.json().catch(() => ({}));
                        return {http_status: res.status, http_ok: res.ok, json};
                    }""",
                    {"presetId": preset_id, "prepared": prepared},
                )
                item["run_response"] = run_result
                if not run_result.get("http_ok") or not run_result.get("json", {}).get("ok"):
                    item["status"] = "run_rejected"
                    item["issues"].append("run_rejected")
                    item["error"] = run_result.get("json", {}).get("msg") or f"HTTP {run_result.get('http_status')}"
                    print(f"[qa] {bundle_id} rejected: {item['error']}", flush=True)
                    continue

                job_id = run_result["json"]["job"]["job_id"]
                item["job_id"] = job_id
                deadline = time.time() + args.per_template_timeout
                last_phase = ""
                while time.time() < deadline:
                    job_payload = browser_api(page, "GET", f"/comfyui/jobs/{job_id}")
                    if job_payload["status"] != 200 or not job_payload["body"].get("ok"):
                        raise RuntimeError(f"job poll failed: {job_payload}")
                    job = job_payload["body"].get("job") or {}
                    progress = job.get("progress") or {}
                    phase = f"{job.get('status')}:{progress.get('phase')}:{progress.get('percent')}"
                    if phase != last_phase:
                        print(f"[qa] {bundle_id} {phase} {str(progress.get('detail') or '')[:160]}", flush=True)
                        last_phase = phase
                    if job.get("status") == "completed" and job.get("result"):
                        item["job"] = job
                        break
                    if job.get("status") == "error":
                        item["status"] = "job_error"
                        item["issues"].append("job_error")
                        item["error"] = job.get("error") or progress.get("detail") or "ComfyUI job error"
                        break
                    time.sleep(max(0.5, args.poll_seconds))
                else:
                    item["status"] = "timeout"
                    item["issues"].append("timeout")
                    item["error"] = f"Timed out after {args.per_template_timeout}s"

                if item["status"] in {"job_error", "timeout"}:
                    print(f"[qa] {bundle_id} failed: {item.get('error')}", flush=True)
                    continue

                result = item.get("job", {}).get("result") or {}
                hydrated = page.evaluate(
                    """async ({jobId, result}) => {
                        const rawImages = Array.isArray(result.images) && result.images.length
                            ? result.images
                            : [result.image].filter(Boolean);
                        const images = await hydrateComfyuiGeneratedImages(rawImages);
                        const media = await hydrateComfyuiGeneratedMedia(Array.isArray(result.media) ? result.media : [], jobId);
                        comfyuiGeneratedImages = images;
                        comfyuiGeneratedMedia = media;
                        renderComfyuiGeneratedImages(comfyuiGeneratedImages);
                        setComfyuiSelectedImage(0);
                        await new Promise((resolve) => setTimeout(resolve, 500));
                        const preview = document.getElementById("comfyui-preview");
                        return {
                            images,
                            media,
                            preview: {
                                text: preview?.textContent?.trim()?.slice(0, 1000) || "",
                                imageElementCount: preview ? preview.querySelectorAll("img").length : 0,
                                videoElementCount: preview ? preview.querySelectorAll("video").length : 0,
                                audioElementCount: preview ? preview.querySelectorAll("audio").length : 0,
                                batchItemCount: preview ? preview.querySelectorAll("[data-comfyui-image-index]").length : 0,
                                outputLabels: Array.from(preview?.querySelectorAll(".comfyui-output-label") || []).map((el) => el.textContent.trim()),
                            },
                        };
                    }""",
                    {"jobId": item["job_id"], "result": result},
                )
                item["preview_dom"] = hydrated.get("preview") or {}
                images = hydrated.get("images") or []
                media = hydrated.get("media") or []
                if not images and not media:
                    item["issues"].append("completed_without_outputs")
                for img_index, image in enumerate(images, 1):
                    data_url = image.get("data_url") or ""
                    if not data_url:
                        item["issues"].append("image_preview_missing_data_url")
                        continue
                    mime, data = decode_data_url(data_url)
                    ext = image_ext_for_mime(mime)
                    image_path = images_dir / f"{slug}_{img_index:02d}{ext}"
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    image_path.write_bytes(data)
                    analysis = analyze_image(image_path)
                    image_record = {
                        "index": img_index,
                        "path": str(image_path),
                        "mime_type": image.get("mime_type") or mime,
                        "size_bytes": len(data),
                        "output_node_id": image.get("output_node_id") or "",
                        "output_label": image.get("output_label") or "",
                        "analysis": analysis.__dict__,
                    }
                    item["images"].append(image_record)
                    if analysis.flags:
                        item["issues"].extend(analysis.flags)
                    contact_items.append({
                        "path": str(image_path),
                        "bundle_id": bundle_id,
                        "label": image.get("output_label") or bundle_id,
                        "prompt": (prepared.get("promptish") or [{}])[0].get("value", ""),
                    })
                for media_index, media_item in enumerate(media, 1):
                    data_url = media_item.get("data_url") or ""
                    record = {
                        "index": media_index,
                        "media_kind": media_item.get("media_kind") or "",
                        "mime_type": media_item.get("mime_type") or "",
                        "size_bytes": media_item.get("size_bytes") or 0,
                        "preview_error": media_item.get("preview_error") or "",
                        "path": "",
                    }
                    if data_url:
                        mime, data = decode_data_url(data_url)
                        path = media_dir / f"{slug}_{media_index:02d}{media_ext_for_mime(media_item.get('mime_type') or mime)}"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(data)
                        record["path"] = str(path)
                        record["size_bytes"] = len(data)
                    if record["preview_error"]:
                        item["issues"].append("media_preview_error")
                    item["media"].append(record)
                try:
                    screenshot_path = screenshots_dir / f"{slug}_preview.png"
                    screenshots_dir.mkdir(parents=True, exist_ok=True)
                    page.locator("#comfyui-preview").screenshot(path=str(screenshot_path), timeout=30_000)
                    item["preview_screenshot"] = str(screenshot_path)
                except Exception as exc:
                    item["issues"].append("preview_screenshot_failed")
                    item["preview_screenshot_error"] = str(exc)
                if item["images"] and item.get("preview_dom", {}).get("imageElementCount", 0) < 1:
                    item["issues"].append("frontend_preview_missing_images")
                if len(item["images"]) > 1 and item.get("preview_dom", {}).get("batchItemCount", 0) < len(item["images"]):
                    item["issues"].append("frontend_gallery_missing_outputs")
                item["status"] = "passed" if not item["issues"] else "completed_with_issues"
                print(f"[qa] {bundle_id} done: {len(item['images'])} images, {len(item['media'])} media, issues={item['issues']}", flush=True)
            except Exception as exc:
                item["status"] = "script_error"
                item["issues"].append("script_error")
                item["error"] = str(exc)
                print(f"[qa] {bundle_id} script error: {exc}", flush=True)
            finally:
                item["duration_seconds"] = round(time.time() - started, 2)
                item["console_events"] = console_events[before_console:]
                item["page_errors"] = page_errors[before_errors:]
                results.append(item)
                (out_dir / "results.partial.json").write_text(
                    json.dumps({"results": results}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

        contact_sheets = build_contact_sheets(contact_items, out_dir)
        browser.close()

    summary = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "comfyui_api_url": args.comfyui_api_url,
        "template_count": len(results),
        "passed": sum(1 for item in results if item["status"] == "passed"),
        "completed_with_issues": sum(1 for item in results if item["status"] == "completed_with_issues"),
        "failed": sum(1 for item in results if item["status"] not in {"passed", "completed_with_issues"}),
        "contact_sheets": contact_sheets,
        "console_event_count": len(console_events),
        "page_error_count": len(page_errors),
        "network_error_count": len(network_errors),
    }
    report = {
        "summary": summary,
        "connection": connection,
        "results": results,
        "console_events": console_events,
        "page_errors": page_errors,
        "network_errors": network_errors,
    }
    (out_dir / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0 if summary["failed"] == 0 and summary["completed_with_issues"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
