"""Fail-closed final composition for SAM3 semantic outpaint.

The corresponding ComfyUI workflow deliberately saves two intermediates: a
Flux-generated full canvas and the original image carrying a SAM3-derived,
foreground-positive alpha channel.  This module is the only place where they
are joined for a user-visible result.
"""

from __future__ import annotations

import hashlib
from io import BytesIO


SEMANTIC_OUTPAINT_FAMILY = "flux_fill_sam3_subject_gguf"
SEMANTIC_BACKGROUND_OUTPUT_NODE_ID = "9"
SEMANTIC_FOREGROUND_OUTPUT_NODE_ID = "124"
_MIN_FOREGROUND_COVERAGE = 0.005
_MAX_FOREGROUND_COVERAGE = 0.65
_COMFYUI_LATENT_GRID_QUANTIZATION = 8


class SemanticCompositeError(RuntimeError):
    """The paired semantic outpaint outputs cannot safely be delivered."""


def _output_item(items, node_id, *, label):
    matches = [
        item for item in items
        if isinstance(item, dict) and str(item.get("output_node_id") or "") == str(node_id)
    ]
    if len(matches) != 1:
        raise SemanticCompositeError(f"SAM3 語意外延缺少唯一的{label}輸出")
    return matches[0]


def _decode_image(data, *, label, require_alpha=False):
    try:
        from PIL import Image
    except Exception as exc:  # pragma: no cover - deployment dependency failure
        raise SemanticCompositeError(f"SAM3 語意外延需要 Pillow：{exc}") from exc
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise SemanticCompositeError(f"SAM3 語意外延{label}圖片內容為空")
    try:
        with Image.open(BytesIO(bytes(data))) as image:
            if require_alpha and "A" not in image.getbands():
                raise SemanticCompositeError(f"SAM3 語意外延{label}未包含透明遮罩")
            return image.convert("RGBA")
    except SemanticCompositeError:
        raise
    except Exception as exc:
        raise SemanticCompositeError(f"SAM3 語意外延{label}圖片無法解析：{exc}") from exc


def _strict_int(value, *, name, default, lower, upper):
    if value in (None, ""):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SemanticCompositeError(f"SAM3 語意外延{name}不合法") from exc
    if parsed < lower or parsed > upper:
        raise SemanticCompositeError(f"SAM3 語意外延{name}必須介於 {lower} 到 {upper}")
    return parsed


def _aligned_canvas_dimension(source_size, before, after):
    """Mirror the trusted blank-canvas alignment used by the route layer.

    The Flux latent is aligned down to eight pixels where that still contains
    the source foreground.  When a trailing-edge crop would clip the subject,
    the route rounds *up* only as far as needed to keep every source pixel.
    Final composition must accept precisely that geometry; accepting a broad
    range here would weaken the fail-closed contract.
    """
    requested = int(source_size) + int(before) + int(after)
    # Tiny synthetic fixtures do not have a Comfy latent grid and are useful
    # for exercising the alpha-compositing safety boundary in unit tests.
    if requested < _COMFYUI_LATENT_GRID_QUANTIZATION:
        return requested
    aligned = (requested // _COMFYUI_LATENT_GRID_QUANTIZATION) * _COMFYUI_LATENT_GRID_QUANTIZATION
    minimum_subject_extent = int(source_size) + int(before)
    if aligned < minimum_subject_extent:
        aligned = (
            (minimum_subject_extent + _COMFYUI_LATENT_GRID_QUANTIZATION - 1)
            // _COMFYUI_LATENT_GRID_QUANTIZATION
        ) * _COMFYUI_LATENT_GRID_QUANTIZATION
    return aligned


def _foreground_alpha(foreground, *, erode_pixels):
    try:
        from PIL import ImageFilter
    except Exception as exc:  # pragma: no cover - deployment dependency failure
        raise SemanticCompositeError(f"SAM3 語意外延需要 Pillow：{exc}") from exc
    alpha = foreground.getchannel("A")
    histogram = alpha.histogram()
    total = max(1, foreground.width * foreground.height)
    coverage = sum(histogram[1:]) / total
    if coverage < _MIN_FOREGROUND_COVERAGE or coverage > _MAX_FOREGROUND_COVERAGE:
        raise SemanticCompositeError(
            "SAM3 前景遮罩覆蓋率不合理，已拒絕交付以避免把背景矩形當成主體"
        )
    if erode_pixels:
        alpha = alpha.filter(ImageFilter.MinFilter(erode_pixels * 2 + 1))
    if alpha.getbbox() is None:
        raise SemanticCompositeError("SAM3 前景遮罩在去邊後為空，已拒絕交付")
    return alpha, coverage


def compose_sam3_outpaint_bundle(background_data, foreground_rgba_data, *, left, top, right, bottom, erode_pixels=1):
    """Return a PNG which places a checked SAM3 foreground over clean Flux art."""
    background = _decode_image(background_data, label="背景")
    foreground = _decode_image(foreground_rgba_data, label="前景", require_alpha=True)
    left = _strict_int(left, name="左側外延", default=0, lower=0, upper=4096)
    top = _strict_int(top, name="上側外延", default=0, lower=0, upper=4096)
    right = _strict_int(right, name="右側外延", default=0, lower=0, upper=4096)
    bottom = _strict_int(bottom, name="下側外延", default=0, lower=0, upper=4096)
    erode_pixels = _strict_int(erode_pixels, name="前景去邊像素", default=1, lower=0, upper=2)
    expected_size = (foreground.width + left + right, foreground.height + top + bottom)
    # The source keeps its original pixels while Flux uses an eight-pixel
    # latent grid.  Mirror the route's exact floor-or-subject-safe-ceil rule,
    # rather than accepting a range that might hide a misplaced canvas.
    expected_canvas_size = (
        _aligned_canvas_dimension(foreground.width, left, right),
        _aligned_canvas_dimension(foreground.height, top, bottom),
    )
    subject_fits = left + foreground.width <= background.width and top + foreground.height <= background.height
    if (
        background.size != expected_canvas_size
        or not subject_fits
    ):
        raise SemanticCompositeError(
            "SAM3 語意外延背景尺寸不符（僅允許已驗證的 ComfyUI 格點對齊）；"
            f"請求 {expected_size[0]}x{expected_size[1]}、對齊後 {expected_canvas_size[0]}x{expected_canvas_size[1]}，"
            f"取得 {background.width}x{background.height}"
        )
    alpha, coverage = _foreground_alpha(foreground, erode_pixels=erode_pixels)
    foreground.putalpha(alpha)
    background.alpha_composite(foreground, (left, top))
    output = BytesIO()
    background.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue(), {
        "background_size": [background.width, background.height],
        "foreground_size": [foreground.width, foreground.height],
        "foreground_coverage": round(coverage, 6),
        "erode_pixels": erode_pixels,
    }


def finalize_sam3_outpaint_result(client, result, params, *, error_cls):
    """Replace paired intermediate outputs with one uploaded product artifact."""
    if not isinstance(result, dict):
        raise error_cls("SAM3 語意外延沒有回傳結果")
    images = result.get("images") if isinstance(result.get("images"), list) else []
    try:
        background_item = _output_item(images, SEMANTIC_BACKGROUND_OUTPUT_NODE_ID, label="乾淨背景")
        foreground_item = _output_item(images, SEMANTIC_FOREGROUND_OUTPUT_NODE_ID, label="SAM3 前景")
        expand = params.get("outpaint") if isinstance(params.get("outpaint"), dict) else {}
        composite_data, report = compose_sam3_outpaint_bundle(
            background_item.get("data"),
            foreground_item.get("data"),
            left=expand.get("left"),
            top=expand.get("top"),
            right=expand.get("right"),
            bottom=expand.get("bottom"),
            # SAM3's binary mask can include a one-pixel strip of an opaque
            # studio backdrop.  Two pixels was visually validated against a
            # white-background source; it removes that matte without widening
            # the configurable safety limit in ``_strict_int``.
            erode_pixels=params.get("outpaint_subject_edge_erode", 2),
        )
        upload = getattr(client, "upload_image_bytes", None)
        if not callable(upload):
            raise SemanticCompositeError("目前 ComfyUI client 無法回寫已驗證的語意外延成品")
        filename_prefix = str(params.get("filename_prefix") or "hackme_web").strip() or "hackme_web"
        filename = f"{filename_prefix}_semantic_outpaint_{hashlib.sha1(composite_data).hexdigest()[:12]}.png"
        image_ref = upload(composite_data, filename, image_type="output", overwrite=False)
        if not isinstance(image_ref, dict) or not str(image_ref.get("filename") or "").strip():
            raise SemanticCompositeError("ComfyUI 未回傳語意外延成品引用")
    except SemanticCompositeError as exc:
        raise error_cls(str(exc)) from exc
    except Exception as exc:
        raise error_cls(f"SAM3 語意外延最終合成/回寫失敗：{exc}") from exc

    image_ref = dict(image_ref)
    image_ref["output_node_id"] = "semantic_composite"
    image_ref["output_label"] = "SAM3 strict semantic composite"
    composite_item = {
        "image_ref": image_ref,
        "mime_type": "image/png",
        "data": composite_data,
        "size_bytes": len(composite_data),
        "output_node_id": "semantic_composite",
        "output_label": "SAM3 strict semantic composite",
    }
    next_result = dict(result)
    next_result.update({
        "image_ref": image_ref,
        "mime_type": "image/png",
        "data": composite_data,
        "images": [composite_item],
        "semantic_outpaint": {
            **report,
            "source_output_nodes": [SEMANTIC_BACKGROUND_OUTPUT_NODE_ID, SEMANTIC_FOREGROUND_OUTPUT_NODE_ID],
            "policy": "strict_alpha_composite",
        },
    })
    return next_result


def finalize_generation_result_if_needed(client, result, params, *, error_cls):
    family = str((params or {}).get("outpaint_workflow_family") or "").strip().lower()
    if family != SEMANTIC_OUTPAINT_FAMILY:
        return result
    return finalize_sam3_outpaint_result(client, result, params, error_cls=error_cls)
