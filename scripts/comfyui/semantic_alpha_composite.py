#!/usr/bin/env python3
"""Explicit, validated foreground alpha compositing for outpaint review.

The ComfyUI graph emits an RGB scene and SAM3 can emit the original source as
RGBA.  This helper makes the alpha polarity, canvas placement and edge-matte
handling observable instead of relying on implicit custom-node mask behavior.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_args():
    parser = argparse.ArgumentParser(description="Composite a SAM3 RGBA foreground onto an outpaint background.")
    parser.add_argument("--background", required=True)
    parser.add_argument("--foreground-rgba", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--x", required=True, type=int)
    parser.add_argument("--y", required=True, type=int)
    parser.add_argument("--erode-pixels", default=1, type=int)
    parser.add_argument("--feather-pixels", default=0.0, type=float)
    parser.add_argument(
        "--alpha-polarity",
        choices=("foreground-positive", "background-positive"),
        required=True,
        help="Whether the supplied RGBA alpha marks the subject or its backdrop.",
    )
    return parser.parse_args()


def _decontaminate_white_matte(image, alpha):
    """Recover RGB where a fractional SAM3 alpha was mixed over white.

    Current SAM3 output is binary, but retaining this step makes a changed
    model's fractional alpha explicit and testable rather than reintroducing a
    white fringe silently.
    """
    from PIL import Image

    rgba = image.convert("RGBA")
    rgb = rgba.convert("RGB")
    alpha_values = alpha.load()
    rgb_values = rgb.load()
    width, height = rgb.size
    fractional = 0
    for y in range(height):
        for x in range(width):
            value = alpha_values[x, y]
            if value in (0, 255):
                continue
            fractional += 1
            opacity = value / 255.0
            red, green, blue = rgb_values[x, y]
            rgb_values[x, y] = tuple(
                max(0, min(255, round((channel - (1.0 - opacity) * 255.0) / opacity)))
                for channel in (red, green, blue)
            )
    return rgb, fractional


def main():
    args = parse_args()
    from PIL import Image, ImageFilter

    background_path = Path(args.background).expanduser().resolve()
    foreground_path = Path(args.foreground_rgba).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)

    background = Image.open(background_path).convert("RGB")
    foreground = Image.open(foreground_path).convert("RGBA")
    alpha = foreground.getchannel("A")
    if args.alpha_polarity == "background-positive":
        from PIL import ImageOps

        alpha = ImageOps.invert(alpha)
    alpha_histogram = alpha.histogram()
    total = foreground.width * foreground.height
    transparent = alpha_histogram[0]
    opaque = alpha_histogram[255]
    fractional = total - transparent - opaque
    if not opaque or transparent == total:
        raise ValueError("SAM3 alpha is not a usable foreground-positive mask")
    if args.x < 0 or args.y < 0 or args.x + foreground.width > background.width or args.y + foreground.height > background.height:
        raise ValueError("foreground placement does not fit inside background canvas")
    if args.erode_pixels < 0 or args.erode_pixels > 8:
        raise ValueError("erode-pixels must be between 0 and 8")
    if args.feather_pixels < 0 or args.feather_pixels > 8:
        raise ValueError("feather-pixels must be between 0 and 8")

    recovered_rgb, fractional_decontaminated = _decontaminate_white_matte(foreground, alpha)
    if args.erode_pixels:
        alpha = alpha.filter(ImageFilter.MinFilter(args.erode_pixels * 2 + 1))
    if args.feather_pixels:
        alpha = alpha.filter(ImageFilter.GaussianBlur(args.feather_pixels))
    recovered_rgba = recovered_rgb.convert("RGBA")
    recovered_rgba.putalpha(alpha)
    composite = background.convert("RGBA")
    composite.alpha_composite(recovered_rgba, (args.x, args.y))
    composite.convert("RGB").save(output_path, format="PNG")
    report = {
        "ok": True,
        "background": str(background_path),
        "foreground_rgba": str(foreground_path),
        "output": str(output_path),
        "background_size": list(background.size),
        "foreground_size": list(foreground.size),
        "placement": {"x": args.x, "y": args.y},
        "alpha": {
            "input_polarity": args.alpha_polarity,
            "normalized_polarity": "foreground-positive",
            "transparent_pixels": transparent,
            "opaque_pixels": opaque,
            "fractional_pixels": fractional,
            "fractional_decontaminated": fractional_decontaminated,
            "erode_pixels": args.erode_pixels,
            "feather_pixels": args.feather_pixels,
        },
        "output_bytes": output_path.stat().st_size,
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
