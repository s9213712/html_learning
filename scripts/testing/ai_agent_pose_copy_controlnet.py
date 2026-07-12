#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any

from PIL import Image
from playwright.sync_api import sync_playwright

from ai_agent_real_i2i_edit_audit import (
    ai_agent_preflight,
    api_fetch,
    detect_visual_artifacts,
    ensure_live_ai_agent_settings,
    first_result_image,
    import_image,
    login,
    open_ai_agent,
    save_preview_with_retry,
    wait_job,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = REPO_ROOT / "output/qwen_squat_double_v_white_longhair_cat_ears.png"
DEFAULT_CLOTHES_REF = Path("/mnt/c/share/ComfyUI/output/test/clothes/purple_sheer_lingerie_set.JPG")
DEFAULT_POSE_REF = Path("/mnt/c/share/ComfyUI/output/test/pose/lying_back_legs_up_pose.JPG")
DEFAULT_CONTROLNET_MODEL = "QWEN\\Qwen-Image-2512-Fun-Controlnet-Union-2602.safetensors"
DEFAULT_POSE_COPY_OUTPUT = REPO_ROOT / "output/qwen_pose_copy_controlnet_lying_legs_up_purple.png"
DEFAULT_MAIN_OUTPUT = REPO_ROOT / "output/qwen_multiref_controlnet_resume_lying_legs_up_purple.png"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def copy_asset(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return str(dst)


def image_summary(path: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        return {"path": str(path), "size": list(image.size), "mode": image.mode}


def imported_record(image: dict[str, Any], *, semantic_key: str = "") -> dict[str, Any]:
    image_ref = image.get("image_ref") if isinstance(image.get("image_ref"), dict) else {}
    filename = image.get("filename") or image_ref.get("filename") or ""
    cloud_file_id = image.get("cloud_file_id") or image_ref.get("cloud_file_id") or ""
    storage_file_id = image.get("storage_file_id") or image_ref.get("storage_file_id") or ""
    mime_type = image.get("mime_type") or image_ref.get("mime_type") or "image/png"
    size_bytes = image.get("size_bytes") if image.get("size_bytes") is not None else image_ref.get("size_bytes")
    merged_ref = {
        **image_ref,
        "filename": filename,
        "cloud_file_id": cloud_file_id,
        "storage_file_id": storage_file_id,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
    }
    record = {
        "filename": filename,
        "cloud_file_id": cloud_file_id,
        "storage_file_id": storage_file_id,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "image_ref": merged_ref,
    }
    if semantic_key:
        record["semantic_key"] = semantic_key
        record["image_ref"]["semantic_key"] = semantic_key
    return record


def print_progress(**payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def make_progress_writer(out_dir: Path, *, job_id: str):
    def _write(poll: dict[str, Any], job: dict[str, Any]) -> None:
        progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
        payload = {
            "phase": poll.get("phase") or "poll",
            "at": poll.get("at"),
            "http_status": poll.get("http_status"),
            "ok": poll.get("ok"),
            "job_id": job_id,
            "job_status": job.get("status") or poll.get("job_status"),
            "percent": poll.get("percent"),
            "detail": poll.get("detail") or progress.get("detail") or progress.get("error_message"),
            "unchanged_seconds": poll.get("unchanged_seconds"),
        }
        write_json(out_dir / "progress.json", payload)
        print_progress(**payload)

    return _write


def build_pose_control_args(
    *,
    pose: dict[str, Any],
    steps: int,
    cfg: float,
    seed: int,
    width: int,
    height: int,
    control_strength: float,
    control_start: float,
    control_end: float,
    profile: str,
    backend_url: str,
) -> dict[str, Any]:
    prompt = (
        "full-color polished anime illustration, clearly adult white-haired cat-ear woman, white long hair, white cat ears, "
        "turquoise eyes, cute gentle face, moonlit Japanese outdoor onsen, warm paper lanterns, full moon, steam and wet stones, "
        "single character reclined on her back, exact composition from the pose reference: torso leaning back, both legs raised upward, "
        "feet near the top corners, soles visible, knees high near shoulders, both hands holding ankles, elbows bent outward, "
        "full body inside frame, strong foreshortening, dynamic but non-explicit fashion editorial pose, "
        "opaque lined purple lace-inspired bodysuit with short dress/tulle accents, covered chest and covered hips, no nudity, "
        "clean finished rendering, detailed shading, soft rim light, no text, no watermark, no signature"
    )
    negative = (
        "line art, sketch, construction drawing, pose map, control map, mannequin, doll joints, grey guide lines, white background, "
        "flat coloring, unfinished, monochrome, wireframe, copied reference sheet, child, loli, teen, underage, young-looking, "
        "nude, naked, nipples, areola, genital, explicit sex, porn, erotic spread, transparent chest, transparent crotch, "
        "cameltoe, upskirt, extra people, extra limbs, missing limbs, broken hands, fused fingers, bad anatomy, distorted legs, "
        "feet cropped, face cropped, text, logo, watermark, signature"
    )
    return {
        "official_workflow_id": "origin_qwen_image_controlnet_2512",
        "generation_mode": "txt2img",
        "prompt": prompt,
        "negative_prompt": negative,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg": cfg,
        "sampler_name": "euler",
        "scheduler": "simple",
        "seed": seed,
        "batch_size": 1,
        "confirm_billing": True,
        "control_image_ref": pose["image_ref"],
        "control_image_ref_json": pose["image_ref"],
        "controlnet": {
            "image_ref": pose["image_ref"],
            "type": "pose",
            "preprocessor": "none",
            "model_name": DEFAULT_CONTROLNET_MODEL,
            "strength": control_strength,
            "start_percent": control_start,
            "end_percent": control_end,
        },
        "controlnet_type": "pose",
        "controlnet_preprocessor": "none",
        "controlnet_model": DEFAULT_CONTROLNET_MODEL,
        "control_strength": control_strength,
        "control_start": control_start,
        "control_end": control_end,
        "qwen_controlnet_profile": profile,
        "backend_url": backend_url.rstrip("/"),
        "comfyui_backend_url": backend_url.rstrip("/"),
        "agent_review_plan": {
            "source_role": "identity prompt target based on source image",
            "clothes_reference_role": "purple covered garment styling only",
            "pose_reference_role": "hard pose/control input",
            "safety": "adult, non-explicit, no nudity, no transparent skin exposure",
        },
    }


def write_markdown_report(out_dir: Path, report: dict[str, Any]) -> Path:
    md_path = out_dir / "AI_AGENT_POSE_COPY_CONTROLNET.md"
    case = report.get("case") if isinstance(report.get("case"), dict) else {}
    args = case.get("write_arguments") if isinstance(case.get("write_arguments"), dict) else {}
    result_rel = case.get("result_image_rel") or ""
    lines = [
        "# AI Agent pose-copy ControlNet",
        "",
        f"- Started: {report.get('started_at')}",
        f"- Finished: {report.get('finished_at') or '-'}",
        f"- Base URL: `{report.get('base_url')}`",
        f"- ComfyUI: `{report.get('comfyui_api_url')}`",
        f"- Source identity reference: `{report.get('source_image')}`",
        f"- Clothes style reference: `{report.get('clothes_ref')}`",
        f"- Pose control reference: `{report.get('pose_ref')}`",
        f"- Workflow: `{args.get('official_workflow_id') or '-'}`",
        f"- Steps/cfg/profile: `{args.get('steps')}` / `{args.get('cfg')}` / `{args.get('qwen_controlnet_profile')}`",
        f"- Control strength/start/end: `{args.get('control_strength')}` / `{args.get('control_start')}` / `{args.get('control_end')}`",
        f"- Job ID: `{case.get('job_id') or '-'}`",
        f"- Job status: `{case.get('job_status') or '-'}`",
        f"- Pose-copy output: `{case.get('pose_copy_output') or ''}`",
        f"- Main output replaced: `{case.get('main_output_copy') or ''}`",
        "",
        "## Routing evidence",
        "",
        "```json",
        json.dumps(
            {
                "control_image_ref": args.get("control_image_ref"),
                "workflow_bridge_adjustments": case.get("workflow_bridge_adjustments"),
                "image_field_assignments": case.get("image_field_assignments"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
        "## Prompt",
        "",
        "```text",
        str(args.get("prompt") or ""),
        "```",
        "",
        "## Result",
        "",
    ]
    if result_rel:
        lines.append(f"![result]({result_rel})")
    else:
        lines.append("No result image was saved.")
    lines.extend([
        "",
        "## Raw write response",
        "",
        "```json",
        json.dumps(case.get("write_response") or {}, ensure_ascii=False, indent=2)[:12000],
        "```",
        "",
    ])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    parser.add_argument("--username", default="root")
    from scripts.testing.probe_credentials import add_root_password_argument
    add_root_password_argument(parser)
    parser.add_argument("--model", default="qwen3.5:cloud")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--comfyui-api-url", default="http://127.0.0.1:8189")
    parser.add_argument("--source-image", default=str(DEFAULT_SOURCE))
    parser.add_argument("--clothes-ref", default=str(DEFAULT_CLOTHES_REF))
    parser.add_argument("--pose-ref", default=str(DEFAULT_POSE_REF))
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--cfg", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=640768203)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--control-strength", type=float, default=0.55)
    parser.add_argument("--control-start", type=float, default=0.0)
    parser.add_argument("--control-end", type=float, default=0.82)
    parser.add_argument("--qwen-controlnet-profile", default="fast")
    parser.add_argument("--job-timeout-seconds", type=int, default=7200)
    parser.add_argument("--stalled-job-seconds", type=int, default=2400)
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    stamp = time.strftime("%Y-%m-%d_%H%M_ai_agent_pose_copy_controlnet")
    out_dir = Path(args.out_dir or (REPO_ROOT / "docs/AGENTS/reports" / stamp)).resolve()
    assets_dir = out_dir / "assets"
    results_dir = out_dir / "results"
    assets_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    source_path = Path(args.source_image).resolve()
    clothes_path = Path(args.clothes_ref).resolve()
    pose_path = Path(args.pose_ref).resolve()
    for path in (source_path, clothes_path, pose_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    report: dict[str, Any] = {
        "ok": False,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": args.base_url.rstrip("/"),
        "comfyui_api_url": args.comfyui_api_url.rstrip("/"),
        "source_image": str(source_path),
        "clothes_ref": str(clothes_path),
        "pose_ref": str(pose_path),
        "assets": {
            "source": copy_asset(source_path, assets_dir / source_path.name),
            "clothes": copy_asset(clothes_path, assets_dir / clothes_path.name),
            "pose": copy_asset(pose_path, assets_dir / pose_path.name),
        },
        "image_summaries": {
            "source": image_summary(source_path),
            "clothes": image_summary(clothes_path),
            "pose": image_summary(pose_path),
        },
    }
    write_json(out_dir / "report.json", report)
    print_progress(phase="start", out_dir=str(out_dir), workflow="origin_qwen_image_controlnet_2512", steps=args.steps)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        try:
            login(page, report["base_url"], args.username, args.root_password)
            report["settings_update"] = ensure_live_ai_agent_settings(
                page,
                model=args.model,
                api_base_url=args.api_base_url,
                comfyui_api_url=args.comfyui_api_url,
            )
            open_ai_agent(page, report["base_url"])
            report["preflight"] = ai_agent_preflight(page)
            report["ai_agent_status"] = api_fetch(page, "GET", "/api/ai-agent/status").get("body")
            report["comfyui_status"] = api_fetch(page, "GET", "/api/comfyui/status").get("body")
            source = imported_record(import_image(page, source_path, source_path.name), semantic_key="source")
            clothes = imported_record(import_image(page, clothes_path, clothes_path.name), semantic_key="clothes")
            pose = imported_record(import_image(page, pose_path, pose_path.name), semantic_key="pose_control")
            report["imported_images"] = {"source": source, "clothes": clothes, "pose": pose}
            write_json(out_dir / "report.json", report)
            print_progress(
                phase="imported",
                source=source.get("cloud_file_id"),
                clothes=clothes.get("cloud_file_id"),
                pose=pose.get("cloud_file_id"),
            )

            write_args = build_pose_control_args(
                pose=pose,
                steps=args.steps,
                cfg=args.cfg,
                seed=args.seed,
                width=args.width,
                height=args.height,
                control_strength=args.control_strength,
                control_start=args.control_start,
                control_end=args.control_end,
                profile=args.qwen_controlnet_profile,
                backend_url=args.comfyui_api_url,
            )
            write_request = {
                "tool": "write_comfyui_generate",
                "confirm": "EXECUTE",
                "arguments": write_args,
            }
            print_progress(
                phase="submit",
                workflow=write_args["official_workflow_id"],
                steps=write_args["steps"],
                control_strength=write_args["control_strength"],
            )
            write_response = api_fetch(page, "POST", "/api/ai-agent/write-tools/execute", write_request)
            response_body = write_response.get("body") if isinstance(write_response.get("body"), dict) else {}
            result_payload = response_body.get("result") if isinstance(response_body.get("result"), dict) else {}
            job_payload = result_payload.get("job") if isinstance(result_payload.get("job"), dict) else {}
            job_id = str(job_payload.get("job_id") or result_payload.get("job_id") or "")
            case: dict[str, Any] = {
                "case_id": "pose_copy_controlnet",
                "title": "AI Agent pose-copy ControlNet",
                "write_request": write_request,
                "write_arguments": write_args,
                "write_response": response_body,
                "workflow_bridge_adjustments": result_payload.get("workflow_bridge_adjustments") or [],
                "image_field_assignments": result_payload.get("image_field_assignments") or {},
                "job_id": job_id,
                "job_status": job_payload.get("status") or "",
                "result_preview": {},
            }
            report["case"] = case
            write_json(out_dir / "report.json", report)
            print_progress(phase="submitted", job_id=job_id, status=case["job_status"], http_status=write_response.get("status"))

            if not response_body.get("ok") or not job_id:
                case["job_status"] = "not_submitted"
                report["error"] = {
                    "stage": "write_tool_submit",
                    "http_status": write_response.get("status"),
                    "body": response_body,
                }
                write_json(out_dir / "report.json", report)
                write_markdown_report(out_dir, report)
                return 2

            job, polls = wait_job(
                page,
                job_id,
                args.job_timeout_seconds,
                stalled_seconds=args.stalled_job_seconds,
                progress_callback=make_progress_writer(out_dir, job_id=job_id),
            )
            case["job"] = job
            case["job_polls"] = polls
            case["job_status"] = job.get("status") or case.get("job_status") or ""
            image = first_result_image(job)
            preview: dict[str, Any] = {}
            if image:
                preview = save_preview_with_retry(page, image["image_ref"], results_dir / "pose_copy_controlnet_result.png")
            case["result_preview"] = preview
            if preview.get("ok"):
                preview_path = Path(preview["path"])
                case["result_image_rel"] = str(preview_path.relative_to(out_dir))
                case["visual_artifacts"] = detect_visual_artifacts(preview_path)
                copy_asset(preview_path, DEFAULT_POSE_COPY_OUTPUT)
                copy_asset(preview_path, DEFAULT_MAIN_OUTPUT)
                case["pose_copy_output"] = str(DEFAULT_POSE_COPY_OUTPUT)
                case["main_output_copy"] = str(DEFAULT_MAIN_OUTPUT)
                report["ok"] = str(case["job_status"]).lower() in {"completed", "completed_pending_result"} and not bool(
                    case["visual_artifacts"].get("has_blocking_artifact")
                )
            else:
                case["visual_artifacts"] = {}
                report["ok"] = False
            report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            write_json(out_dir / "report.json", report)
            md_path = write_markdown_report(out_dir, report)
            print_progress(
                phase="finished",
                ok=report["ok"],
                job_status=case["job_status"],
                result=case.get("result_preview", {}).get("path"),
                pose_copy_output=case.get("pose_copy_output"),
                main_output=case.get("main_output_copy"),
                report=str(md_path),
            )
            return 0 if report["ok"] else 1
        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
