from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from PIL import Image


def _load_i2i_audit_module():
    repo = Path(__file__).resolve().parents[2]
    script = repo / "scripts" / "testing" / "ai_agent_real_i2i_edit_audit.py"
    spec = importlib.util.spec_from_file_location("ai_agent_real_i2i_edit_audit", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_i2i_source_fixture_keeps_prompt_in_metadata_not_pixels(tmp_path):
    module = _load_i2i_audit_module()
    paths = module.make_fixture_images(tmp_path)

    metadata = json.loads(Path(paths["source_metadata"]).read_text(encoding="utf-8"))
    assert metadata["base_prompt"] == "by ogipote, anime style, 1girl"
    assert metadata["visible_prompt_text_in_image"] is False

    image = Image.open(paths["source"]).convert("RGB")
    top_label_strip = image.crop((0, 0, 760, 76))
    dark_pixels = sum(1 for r, g, b in module.image_pixels(top_label_strip) if r < 100 and g < 100 and b < 100)
    assert dark_pixels == 0
