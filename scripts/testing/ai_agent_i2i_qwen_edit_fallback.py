#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.dont_write_bytecode = True

from scripts.test_artifacts import test_artifact_path  # noqa: E402

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


DEFAULT_SOURCE = REPO_ROOT / "scripts/testing/fixtures/ai_agent/qwen_squat_double_v_white_longhair_cat_ears.png"


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


def build_qwen_edit_args(
    *,
    source: dict[str, Any],
    clothes: dict[str, Any],
    steps: int,
    cfg: float,
    seed: int,
    width: int,
    height: int,
    backend_url: str,
    cleanup_pose_guides: bool = False,
) -> dict[str, Any]:
    if cleanup_pose_guides:
        edit_instruction = (
            "Clean up this pose-controlled image into a finished full-color anime illustration. "
            "Remove every visible construction guide line, purple skeleton line, joint dot, mannequin outline, wireframe mark, and pose-control marking from the character. "
            "Preserve the exact lying-on-back pose with both legs raised upward, feet high, hands holding ankles, white long hair, white cat ears, turquoise eyes, and moonlit Japanese onsen setting. "
            "Use the reference image only for outfit color and garment design: purple lace and tulle styling, reinterpreted as an opaque lined modest purple bodysuit or short dress. "
            "Keep the result non-explicit, fully covered, editorial, polished, and fully rendered; do not change the pose back to squatting."
        )
    else:
        edit_instruction = (
            "Transform the same clearly adult white-haired cat-ear anime woman into a lying-on-her-back pose with both legs raised upward. "
            "Preserve the source character identity, white long hair, white cat ears, turquoise eyes, gentle cute face, and moonlit Japanese onsen setting. "
            "Use the reference image only for the outfit color and garment design: purple lace and tulle styling, reinterpreted as an opaque lined modest purple bodysuit or short dress. "
            "Do not copy the reference image background, mannequin, product layout, text, face, or identity. "
            "Keep the result non-explicit, covered, editorial, and polished anime illustration."
        )
    prompt = (
        "by ogipote anime style tag only, polished detailed anime illustration, moonlit outdoor onsen, warm paper lanterns, full moon, "
        "single adult character, safe fashion editorial, no visible text, no signature, no watermark, no logo"
    )
    negative = (
        "construction guide lines, purple skeleton lines, pose map, control map, joint dots, mannequin outline, wireframe, sketch, line art, "
        "child, loli, teen, underage, young-looking, nude, naked, nipples, areola, genital, explicit sex, porn, erotic spread, "
        "transparent chest, transparent crotch, cameltoe, upskirt, product photo, mannequin, clothes only, empty scene, extra people, "
        "extra limbs, missing limbs, broken hands, fused fingers, bad anatomy, distorted legs, feet cropped, face cropped, text, logo, watermark, signature"
    )
    return {
        "official_workflow_id": "origin_qwen_image_edit_2509",
        "generation_mode": "img2img",
        "source_image_ref": source["image_ref"],
        "source_image_ref_json": source["image_ref"],
        "reference_image_ref": clothes["image_ref"],
        "reference_image_ref_json": clothes["image_ref"],
        "qwen_reference_mode": "stage_guarded_image2",
        "qwen_reference_image2": True,
        "qwen_reference_force_image2": True,
        "qwen_edit_profile": "fast",
        "edit_instruction": edit_instruction,
        "prompt": prompt,
        "negative_prompt": negative,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg": cfg,
        "sampler_name": "euler",
        "scheduler": "simple",
        "seed": seed,
        "denoise_strength": 1,
        "batch_size": 1,
        "confirm_billing": True,
        "backend_url": backend_url.rstrip("/"),
        "comfyui_backend_url": backend_url.rstrip("/"),
    }


def write_markdown_report(out_dir: Path, report: dict[str, Any]) -> Path:
    md_path = out_dir / "AI_AGENT_QWEN_EDIT_I2I_FALLBACK.md"
    case = report.get("case") if isinstance(report.get("case"), dict) else {}
    args = case.get("write_arguments") if isinstance(case.get("write_arguments"), dict) else {}
    result_rel = case.get("result_image_rel") or ""
    lines = [
        "# AI Agent Qwen Edit i2i fallback",
        "",
        f"- Started: {report.get('started_at')}",
        f"- Finished: {report.get('finished_at') or '-'}",
        f"- Base URL: `{report.get('base_url')}`",
        f"- ComfyUI: `{report.get('comfyui_api_url')}`",
        f"- Source: `{report.get('source_image')}`",
        f"- Clothes reference: `{report.get('clothes_ref')}`",
        f"- Pose reference text role: `{report.get('pose_ref')}`",
        f"- Workflow: `{args.get('official_workflow_id') or '-'}`",
        f"- Steps/cfg/profile: `{args.get('steps')}` / `{args.get('cfg')}` / `{args.get('qwen_edit_profile')}`",
        f"- Job ID: `{case.get('job_id') or '-'}`",
        f"- Job status: `{case.get('job_status') or '-'}`",
        f"- Result path: `{case.get('fallback_output_copy') or ''}`",
        f"- Legacy interrupted-output replacement: `{case.get('replaced_controlnet_output') or ''}`",
        "",
        "## ControlNet failure carried forward",
        "",
        "The interrupted ControlNet resume completed but saved a purple pose/control drawing instead of a generated anime image. "
        "That artifact is preserved in `results/controlnet_failed_pose_output.png` when available.",
        "",
        "## Routing evidence",
        "",
        "```json",
        json.dumps(
            {
                "source_image_ref": args.get("source_image_ref"),
                "reference_image_ref": args.get("reference_image_ref"),
                "workflow_bridge_adjustments": case.get("workflow_bridge_adjustments"),
                "image_field_assignments": case.get("image_field_assignments"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        "```",
        "",
        "## Edit Instruction",
        "",
        "```text",
        str(args.get("edit_instruction") or ""),
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
    parser.add_argument("--clothes-ref", required=True)
    parser.add_argument("--pose-ref", required=True)
    parser.add_argument(
        "--controlnet-result",
        default="",
        help="Optional prior ControlNet result to preserve with the report.",
    )
    parser.add_argument("--steps", type=int, default=4)
    parser.add_argument("--cfg", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=640768202)
    parser.add_argument("--width", type=int, default=1024)
    parser.add_argument("--height", type=int, default=1024)
    parser.add_argument("--cleanup-pose-guides", action="store_true")
    parser.add_argument("--job-timeout-seconds", type=int, default=7200)
    parser.add_argument("--stalled-job-seconds", type=int, default=2400)
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    stamp = time.strftime("%Y-%m-%d_%H%M_ai_agent_qwen_edit_i2i_fallback")
    out_dir = Path(args.out_dir).resolve() if args.out_dir else test_artifact_path("reports", stamp).resolve()
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
    controlnet_result = Path(args.controlnet_result).expanduser().resolve() if args.controlnet_result else None
    if controlnet_result and controlnet_result.is_file():
        report["controlnet_failed_pose_output"] = copy_asset(
            controlnet_result,
            results_dir / "controlnet_failed_pose_output.png",
        )
    write_json(out_dir / "report.json", report)
    print_progress(phase="start", out_dir=str(out_dir), workflow="origin_qwen_image_edit_2509", steps=args.steps)

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
            pose = imported_record(import_image(page, pose_path, pose_path.name), semantic_key="pose")
            report["imported_images"] = {"source": source, "clothes": clothes, "pose": pose}
            write_json(out_dir / "report.json", report)
            print_progress(
                phase="imported",
                source=source.get("cloud_file_id"),
                clothes=clothes.get("cloud_file_id"),
                pose=pose.get("cloud_file_id"),
            )

            write_args = build_qwen_edit_args(
                source=source,
                clothes=clothes,
                steps=args.steps,
                cfg=args.cfg,
                seed=args.seed,
                width=args.width,
                height=args.height,
                backend_url=args.comfyui_api_url,
                cleanup_pose_guides=args.cleanup_pose_guides,
            )
            write_request = {
                "tool": "write_comfyui_generate",
                "confirm": "EXECUTE",
                "arguments": write_args,
            }
            print_progress(phase="submit", workflow=write_args["official_workflow_id"], steps=write_args["steps"])
            write_response = api_fetch(page, "POST", "/api/ai-agent/write-tools/execute", write_request)
            response_body = write_response.get("body") if isinstance(write_response.get("body"), dict) else {}
            result_payload = response_body.get("result") if isinstance(response_body.get("result"), dict) else {}
            job_payload = result_payload.get("job") if isinstance(result_payload.get("job"), dict) else {}
            job_id = str(job_payload.get("job_id") or result_payload.get("job_id") or "")
            case: dict[str, Any] = {
                "case_id": "qwen_edit_pose_cleanup" if args.cleanup_pose_guides else "qwen_edit_i2i_fallback",
                "title": "AI Agent Qwen Edit pose cleanup" if args.cleanup_pose_guides else "AI Agent Qwen Edit i2i fallback",
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
                preview = save_preview_with_retry(page, image["image_ref"], results_dir / "qwen_edit_i2i_fallback_result.png")
            case["result_preview"] = preview
            if preview.get("ok"):
                preview_path = Path(preview["path"])
                case["result_image_rel"] = str(preview_path.relative_to(out_dir))
                case["visual_artifacts"] = detect_visual_artifacts(preview_path)
                case["fallback_output_copy"] = str(preview_path)
                case["replaced_controlnet_output"] = ""
                if args.cleanup_pose_guides:
                    case["pose_cleanup_output_copy"] = str(preview_path)
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
                fallback_output=case.get("fallback_output_copy"),
                replaced_controlnet_output=case.get("replaced_controlnet_output"),
                pose_cleanup_output=case.get("pose_cleanup_output_copy"),
                report=str(md_path),
            )
            return 0 if report["ok"] else 1
        finally:
            ctx.close()
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
