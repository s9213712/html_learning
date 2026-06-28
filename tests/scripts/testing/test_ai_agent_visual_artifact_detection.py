from pathlib import Path
import sys

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts" / "testing"))

from ai_agent_real_i2i_edit_audit import detect_visual_artifacts  # noqa: E402


def test_visual_artifact_detector_allows_gray_studio_background(tmp_path):
    path = tmp_path / "studio_background.png"
    image = Image.new("RGB", (512, 512))
    pixels = image.load()
    for y in range(512):
        for x in range(512):
            value = int(132 + 34 * (x / 511) - 18 * (y / 511))
            pixels[x, y] = (value, value + 1, value + 2)
    draw = ImageDraw.Draw(image)
    draw.ellipse((165, 70, 345, 250), fill=(240, 188, 170))
    draw.rectangle((135, 250, 375, 500), fill=(225, 205, 190))
    draw.rectangle((190, 60, 320, 150), fill=(20, 28, 70))
    image.save(path)

    result = detect_visual_artifacts(path)

    assert result["checked"] is True
    assert result["largest_gray_component_ratio"] >= 0.08
    assert result["has_blocking_artifact"] is False
    assert result["reason"] == ""


def test_visual_artifact_detector_blocks_flat_gray_frame(tmp_path):
    path = tmp_path / "gray_frame.png"
    image = Image.new("RGB", (512, 512), (142, 142, 142))
    draw = ImageDraw.Draw(image)
    draw.rectangle((120, 120, 392, 392), fill=(230, 198, 178))
    draw.ellipse((205, 145, 307, 247), fill=(35, 45, 92))
    image.save(path)

    result = detect_visual_artifacts(path)

    assert result["checked"] is True
    assert result["suspicious_uniform_gray_frame"] is True
    assert result["has_blocking_artifact"] is True
    assert result["reason"] == "large uniform gray frame"


def test_visual_artifact_detector_blocks_nearly_black_image(tmp_path):
    path = tmp_path / "black.png"
    Image.new("RGB", (128, 128), (0, 0, 0)).save(path)

    result = detect_visual_artifacts(path)

    assert result["has_blocking_artifact"] is True
    assert result["reason"] == "image is nearly all black"
