#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_PROMPT = "by ogipote, anime style, 1girl"
IMAGE_WIDTH = 1024
IMAGE_HEIGHT = 1024
RESOLUTION_LABEL = f"{IMAGE_WIDTH}x{IMAGE_HEIGHT}"
SOURCE_IMAGE_NAME = "source_1024x1024.png"
DEFAULT_AI_AGENT_MODEL = "qwen3.5:cloud"
DEFAULT_AI_AGENT_API_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_COMFYUI_API_URL = "http://127.0.0.1:8189"
SOURCE_SCENE_PROMPT = (
    f"{BASE_PROMPT}, medium-wide anime classroom tabletop test scene, a centered girl standing calmly in the background behind a wooden table, "
    "full face visible in upper center, both eyes open, visible nose and mouth, gentle smile, long auburn hair, shoulders visible, arms lowered behind the table, "
    "large empty wooden tabletop foreground, a single red apple isolated on the far left side of the table, a single plain blue ceramic mug isolated on the far right side of the table, "
    "wide empty gap between the apple and the mug, no other tabletop objects, clean anime line art, detailed SDXL illustration, no text, no watermark, no logo, no symbols, no object pattern"
)

SOURCE_GENERATION_CASE: dict[str, Any] = {
    "case_id": "00_txt2img_source",
    "title": "Text-to-image source generation",
    "expected": (
        "由 AI agent 以自然語言指令產生 1024x1024 SDXL 等級原圖；畫面應為 anime 1girl 在教室木桌前，"
        "桌上有左下紅蘋果與右下藍色杯子，右側有可供修正測試的手部異常，不應出現提示詞文字。"
    ),
    "natural_language": (
        "請真的用本站 ComfyUI 文生圖產生基底原圖，generation_mode=txt2img，解析度 1024x1024，SDXL 等級，steps 24，batch 1，confirm_billing=true。"
        "正向提示詞請以 `by ogipote, anime style, 1girl` 為基礎，但這張只做物件 i2i source，不測手部。"
        "請畫 medium-wide anime classroom tabletop test scene：女孩在背景、木桌在前景，臉在上半部中央且清楚可見，both eyes open, visible nose and mouth, gentle smile。"
        "女孩的手臂自然垂下並被桌面擋住，不要讓手成為畫面焦點；重點是桌面前景的兩個測試物件。"
        "桌面前景要大而清楚，中央留空 wide empty gap。viewer-left/far left tabletop 必須是一顆 isolated single red apple；viewer-right/far right tabletop 必須是一個 isolated single plain blue ceramic mug。"
        "不要中央杯子、不要白色高腳杯、不要花瓶、不要粉紅杯子、不要盒子、不要第二個杯子、不要其他桌面物件。"
        "不要有文字、水印、logo、符號、杯身圖案、黑板字、相框、椅背或水平桿穿過身體。"
        "負面提示詞請包含 hands covering eyes, hands on face, hands touching face, hands on cheeks, chin resting on hands, hand over face, covering mouth, closed eyes, lying down, leaning on table, face too low, cropped head, no face, missing eyes, cropped above nose, faceless, hidden face, hair covering eyes, cup in center, central cup, goblet, white cup, vase, apple on right, blue cup on left, pink cup, box, second cup, extra object, giant apple, cup text, cup logo, symbols, watermark, logo, text, picture frame。"
        "請依語意整理成可執行的 ComfyUI write-tool 參數並送出。"
    ),
}

PERSON_I2I_SOURCE_SCENE_PROMPT = (
    f"{BASE_PROMPT}, clean half-body character edit benchmark, one centered anime girl standing upright in a bright classroom, "
    "full unobstructed face, visible mouth and chin, both eyes open, gentle smile, auburn shoulder-length hair with a small blue hairpin, "
    "clear red ribbon at collar, white blouse, navy cardigan, visible shoulders, torso, both arms, both hands with five fingers, "
    "simple bracelet on one wrist, small necklace, no table blocking face or body, no text, no watermark, no logo"
)

PERSON_I2I_SOURCE_GENERATION_CASE: dict[str, Any] = {
    "case_id": "00_txt2img_person_i2i_source",
    "title": "Text-to-image person i2i source generation",
    "expected": (
        "由 AI agent 以自然語言指令產生 1024x1024 人物 i2i 基底圖；必須完整露出臉、嘴巴、下巴、上半身、衣服、髮飾、配件與雙手，"
        "不得有桌子或物件遮擋臉與身體，作為後續表情、髮型、髮色、衣服、髮飾、配件、手部修正測試來源。"
    ),
    "natural_language": (
        "請真的用本站 ComfyUI 文生圖產生一張人物 i2i 測試基底圖，generation_mode=txt2img，解析度 1024x1024，SDXL 等級，steps 24，batch 1，confirm_billing=true。"
        "正向提示詞請以 `by ogipote, anime style, 1girl` 為基礎。"
        "畫面必須是 clean half-body character edit benchmark：一位 centered anime girl 站直在明亮教室中，臉部完整无遮挡，full unobstructed face, visible mouth and chin, both eyes open, gentle smile。"
        "需要清楚可編輯的人物元素：auburn shoulder-length hair、small blue hairpin、clear red ribbon at collar、white blouse、navy cardigan、small necklace、simple bracelet。"
        "上半身要完整可見：visible shoulders, torso, both arms, both hands with five fingers；手不要擋臉、不要放在嘴巴或下巴前。"
        "不要桌子遮住臉或身體，不要杯子、蘋果、花瓶、黑板字、相框、椅背或水平桿穿過身體；不要裁切頭、不要裁切手、不要缺手指、不要多手指。"
        "負面提示詞請包含 table covering face, table covering body, hands on face, hand over mouth, hand on chin, chin resting on hands, face too low, cropped head, cropped hands, missing hands, extra fingers, six fingers, bad hands, fused fingers, broken arms, hidden mouth, hidden chin, hair covering eyes, closed eyes, text, watermark, logo, symbols, cup, apple, vase, picture frame, chair back crossing body, horizontal bar through body。"
        "請依語意整理成可執行的 ComfyUI write-tool 參數並送出。"
    ),
}


CASES: list[dict[str, Any]] = [
    {
        "case_id": "01_inpaint_apple_to_plant",
        "title": "Inpaint: apple to plant",
        "mask_key": "apple",
        "expected": "只把畫面中可見的紅蘋果區域改成小盆栽；人物、背景與桌面其他物件應保持穩定。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖的局部重繪 inpaint。使用我剛剛提供的測試原圖當 source，"
            "使用標記為 apple object mask 的遮罩當 mask，把遮罩內桌上的紅蘋果改成一個小盆栽。"
            "解析度 1024x1024，SDXL 等級，batch 1。提示詞基礎：by ogipote, anime style, 1girl；"
            "請加入 suitable anime SDXL quality details，保持人物構圖與其他區域，不要改動整張圖。"
        ),
    },
    {
        "case_id": "02_outpaint_zoomout",
        "title": "Outpaint: zoom-out background",
        "mask_key": None,
        "expected": "四周補出更多背景，呈現 zoom-out/outpaint 效果；主體仍是原本 1girl，不應被替換。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖的 outpaint/向外延展。使用我剛剛提供的測試原圖當 source，"
            "四周各外延 128px，feathering 48，最後仍以 1024x1024、SDXL 等級、batch 1 作為本次輸出規格。"
            "提示詞基礎：by ogipote, anime style, 1girl, zoom-out, extended classroom background；"
            "請延續原本背景、牆面、桌面、裁切到邊緣的物件和光線，不要換掉人物；若原圖邊緣有被裁切的頭髮、桌面或桌面物件，"
            "外延後應自然補出被裁切部分。外延區域不能是灰色填充框、空白邊框或純色 padding，必須看起來像原圖背景自然向外擴展。"
        ),
    },
    {
        "case_id": "03_remove_apple",
        "title": "Remove object: apple",
        "mask_key": "apple",
        "expected": "紅蘋果被移除，該區域自然補成桌面/背景；人物和藍色杯子仍保留。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖的局部重繪 inpaint。使用測試原圖當 source，"
            "使用 apple object mask 遮罩，把桌上的紅蘋果完全移除，補成自然桌面與背景。"
            "解析度 1024x1024，SDXL 等級，batch 1。提示詞基礎：by ogipote, anime style, 1girl；"
            "重點是 remove object，不要新增其他物件，不要改人物。"
        ),
    },
    {
        "case_id": "04_replace_cup_with_plush",
        "title": "Replace object: cup to plush",
        "mask_key": "cup",
        "expected": "藍色杯子及杯身文字/logo 被替換成白色小貓玩偶；其他區域保持穩定。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖的局部重繪 inpaint。使用測試原圖當 source，"
            "使用 cup object mask 遮罩，把桌上的藍色杯子及杯身文字/logo 替換成白色小貓玩偶。"
            "解析度 1024x1024，SDXL 等級，batch 1。提示詞基礎：by ogipote, anime style, 1girl；"
            "只替換杯子，不要改人物臉和構圖。"
        ),
    },
    {
        "case_id": "05_fix_hand_anomaly",
        "title": "Fix anomaly: hand",
        "mask_key": "hand",
        "expected": "右側異常多指手被修成自然手型；人物臉、衣服和背景不應大幅改變。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖的局部重繪 inpaint。使用測試原圖當 source，"
            "使用 hand anomaly mask 遮罩，修正右側手部多指與扭曲問題，變成自然 anime hand。"
            "解析度 1024x1024，SDXL 等級，batch 1。提示詞基礎：by ogipote, anime style, 1girl；"
            "目標是修正異常，不是重畫整個人物。"
        ),
    },
    {
        "case_id": "06_repair_six_finger_to_five",
        "title": "Repair: six fingers to five fingers",
        "mask_key": "hand",
        "expected": "明確的六指手被修成五指手；手掌、袖口、背景和整體畫風不應大幅改變。",
        "source_prompt": (
            f"{BASE_PROMPT}, close-up anime hand repair fixture, one visible right hand on a simple tabletop, "
            "deliberately six fingers before repair, neutral background, no text, no watermark"
        ),
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖的局部重繪 inpaint。使用測試原圖當 source，"
            "這張 source 是一張明確的失敗圖：右手有六根手指。使用 hand anomaly mask 遮罩，"
            "只移除多出來的第六根手指並把手修成自然五指 anime hand。解析度 1024x1024，SDXL 等級，batch 1。"
            "提示詞基礎：by ogipote, anime style, 1girl；不要重畫整隻手，不要改袖口、手掌、背景或構圖。"
        ),
    },
]


def _font(size: int = 28) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def make_fixture_images(out_dir: Path) -> dict[str, Path]:
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    fixture_width = 1024
    fixture_height = 1024
    source = Image.new("RGB", (fixture_width, fixture_height), (238, 232, 222))
    draw = ImageDraw.Draw(source)
    # Background and table.
    draw.rectangle([0, 0, 1024, 670], fill=(232, 238, 244))
    draw.rectangle([0, 670, 1024, 1024], fill=(180, 130, 86))
    draw.rectangle([80, 84, 944, 590], outline=(180, 190, 204), width=6)
    draw.line([90, 270, 934, 270], fill=(205, 212, 220), width=3)
    draw.line([90, 430, 934, 430], fill=(205, 212, 220), width=3)

    # Simple anime 1girl fixture.
    draw.ellipse([352, 120, 672, 440], fill=(252, 218, 190), outline=(110, 80, 70), width=5)
    draw.pieslice([312, 58, 712, 410], start=180, end=360, fill=(245, 202, 88), outline=(120, 92, 42), width=5)
    draw.polygon([(330, 115), (408, 52), (464, 122)], fill=(245, 202, 88), outline=(120, 92, 42))
    draw.polygon([(560, 122), (622, 52), (698, 115)], fill=(245, 202, 88), outline=(120, 92, 42))
    draw.ellipse([420, 248, 466, 292], fill=(70, 100, 150))
    draw.ellipse([558, 248, 604, 292], fill=(70, 100, 150))
    draw.arc([462, 304, 562, 370], start=20, end=160, fill=(150, 80, 100), width=5)
    draw.rectangle([452, 438, 572, 500], fill=(252, 218, 190))
    draw.polygon([(360, 500), (664, 500), (752, 710), (272, 710)], fill=(80, 150, 155), outline=(40, 90, 95))
    draw.polygon([(418, 510), (512, 646), (606, 510)], fill=(244, 244, 238), outline=(120, 120, 110))

    # Deliberate objects and anomaly.
    draw.ellipse([156, 724, 276, 840], fill=(196, 42, 36), outline=(96, 22, 18), width=5)
    draw.rectangle([212, 700, 224, 740], fill=(93, 60, 34))
    draw.ellipse([228, 706, 270, 732], fill=(52, 150, 72))
    draw.rounded_rectangle([752, 728, 878, 860], radius=28, fill=(52, 122, 210), outline=(24, 66, 130), width=5)
    draw.arc([850, 764, 930, 832], start=270, end=90, fill=(24, 66, 130), width=10)
    # Weird six-finger hand on the right.
    draw.ellipse([690, 520, 786, 604], fill=(252, 218, 190), outline=(110, 80, 70), width=4)
    for x in [700, 720, 740, 760, 780, 800]:
        draw.rounded_rectangle([x, 462, x + 20, 540], radius=10, fill=(252, 218, 190), outline=(110, 80, 70), width=3)

    source = source.resize((IMAGE_WIDTH, IMAGE_HEIGHT), Image.Resampling.LANCZOS)
    source_path = assets / SOURCE_IMAGE_NAME
    source.save(source_path)
    metadata_path = assets / "source_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "base_prompt": BASE_PROMPT,
                "source_scene_prompt": SOURCE_SCENE_PROMPT,
                "visible_prompt_text_in_image": False,
                "note": f"Prompt/style tags are metadata for the agent and report only; they are not drawn into {SOURCE_IMAGE_NAME}.",
                "source_resolution": RESOLUTION_LABEL,
                "test_targets": ["apple", "cup", "hand_anomaly", "outpaint_background"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    masks: dict[str, tuple[int, int, int, int, str]] = {
        "apple": (130, 680, 310, 880, "apple object mask"),
        "cup": (720, 700, 940, 900, "cup object mask"),
        "hand": (660, 430, 850, 635, "hand anomaly mask"),
    }
    paths = {"source": source_path, "source_metadata": metadata_path}
    for key, (x1, y1, x2, y2, label) in masks.items():
        sx = IMAGE_WIDTH / fixture_width
        sy = IMAGE_HEIGHT / fixture_height
        x1, y1, x2, y2 = [int(value) for value in (x1 * sx, y1 * sy, x2 * sx, y2 * sy)]
        mask = Image.new("L", (IMAGE_WIDTH, IMAGE_HEIGHT), 0)
        md = ImageDraw.Draw(mask)
        md.rectangle([x1, y1, x2, y2], fill=255)
        mask_rgb = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), (0, 0, 0))
        mask_rgb.paste(Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), (255, 255, 255)), mask=mask)
        overlay = source.copy()
        od = ImageDraw.Draw(overlay, "RGBA")
        od.rectangle([x1, y1, x2, y2], outline=(255, 0, 0, 255), width=8)
        od.rectangle([x1, max(0, y1 - 42), min(IMAGE_WIDTH, x1 + 360), y1], fill=(255, 255, 255, 220))
        od.text((x1 + 8, max(0, y1 - 36)), label, fill=(190, 0, 0, 255), font=_font(24))
        mask_path = assets / f"mask_{key}_{RESOLUTION_LABEL}.png"
        overlay_path = assets / f"mask_{key}_overlay.png"
        mask_rgb.save(mask_path)
        overlay.save(overlay_path)
        paths[f"mask_{key}"] = mask_path
        paths[f"mask_{key}_overlay"] = overlay_path
    return paths


def make_six_finger_repair_fixture(out_dir: Path) -> dict[str, Path]:
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)

    source = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), (234, 228, 218))
    draw = ImageDraw.Draw(source)
    draw.rectangle([0, 0, IMAGE_WIDTH, 610], fill=(230, 235, 242))
    draw.rectangle([0, 610, IMAGE_WIDTH, IMAGE_HEIGHT], fill=(176, 126, 82))
    for y in [670, 760, 850]:
        draw.line([0, y, IMAGE_WIDTH, y + 26], fill=(140, 90, 52), width=4)

    skin = (252, 218, 190)
    skin_shadow = (235, 178, 150)
    line = (112, 76, 68)
    sleeve = (36, 82, 158)
    cuff = (245, 246, 250)

    draw.rounded_rectangle([100, 688, 350, 900], radius=38, fill=sleeve, outline=(16, 42, 90), width=5)
    draw.rounded_rectangle([315, 670, 430, 915], radius=28, fill=cuff, outline=(110, 118, 130), width=4)
    draw.rounded_rectangle([390, 610, 660, 850], radius=84, fill=skin, outline=line, width=6)

    # Five intended fingers plus one deliberately extra sixth finger.
    finger_boxes = [
        (410, 280, 462, 640),
        (472, 240, 526, 632),
        (535, 250, 589, 642),
        (596, 292, 650, 668),
        (648, 362, 704, 716),
        (704, 446, 760, 752),  # extra sixth finger, target for repair
    ]
    for idx, box in enumerate(finger_boxes):
        fill = (255, 226, 200) if idx < 5 else (255, 210, 185)
        draw.rounded_rectangle(box, radius=26, fill=fill, outline=line, width=5)
        x1, y1, x2, y2 = box
        draw.arc([x1 + 12, y2 - 42, x2 - 10, y2 - 12], 0, 180, fill=skin_shadow, width=3)
        draw.line([x1 + 16, y1 + 86, x2 - 18, y1 + 88], fill=skin_shadow, width=2)

    draw.line([438, 660, 622, 650], fill=skin_shadow, width=4)
    draw.line([455, 724, 615, 724], fill=skin_shadow, width=3)
    source_path = assets / "six_finger_repair_source_1024x1024.png"
    source.save(source_path)
    metadata_path = assets / "source_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "base_prompt": BASE_PROMPT,
                "source_scene_prompt": "controlled six-finger hand repair fixture",
                "visible_prompt_text_in_image": False,
                "source_resolution": RESOLUTION_LABEL,
                "test_targets": ["six_finger_hand_repair"],
                "expected_defect": "right hand visibly has six fingers; the rightmost highlighted extra finger is the repair target",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"source": source_path, "source_metadata": metadata_path}


def make_mask_assets_for_source(out_dir: Path, source_path: Path, *, mask_preset: str = "default") -> dict[str, Path]:
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    source = Image.open(source_path).convert("RGB").resize((IMAGE_WIDTH, IMAGE_HEIGHT), Image.Resampling.LANCZOS)
    source_out = assets / SOURCE_IMAGE_NAME
    if source_path.resolve() != source_out.resolve():
        source.save(source_out)
    metadata_path = assets / "source_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "base_prompt": BASE_PROMPT,
                "source_scene_prompt": SOURCE_SCENE_PROMPT,
                "source_generation_case": SOURCE_GENERATION_CASE["case_id"],
                "visible_prompt_text_in_image": False,
                "note": f"{SOURCE_IMAGE_NAME} is generated by the live AI agent through ComfyUI txt2img; prompt/style tags are not intended as visible image text.",
                "source_resolution": RESOLUTION_LABEL,
                "test_targets": ["apple", "cup", "hand_anomaly", "outpaint_background"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    base_width, base_height = 1920, 1080
    if mask_preset in {"six_finger_wide_bg", "six_finger_repair_wide_bg"}:
        base_width, base_height = IMAGE_WIDTH, IMAGE_HEIGHT
        base_masks = {
            "apple": (32, 32, 96, 96, "unused apple mask"),
            "cup": (112, 32, 176, 96, "unused cup mask"),
            "hand": (670, 396, 820, 806, "extra sixth finger plus background mask"),
        }
    elif mask_preset in {"six_finger_fixture", "six_finger_repair"}:
        base_width, base_height = IMAGE_WIDTH, IMAGE_HEIGHT
        base_masks = {
            "apple": (32, 32, 96, 96, "unused apple mask"),
            "cup": (112, 32, 176, 96, "unused cup mask"),
            "hand": (688, 424, 784, 778, "extra sixth finger mask"),
        }
    elif mask_preset in {"accepted_v5_fingertips", "v5_fingertips"}:
        base_masks = {
            "apple": (1020, 380, 1430, 680, "apple object mask"),
            "cup": (700, 595, 1135, 900, "cup object mask"),
            "hand": (1110, 545, 1680, 870, "finger-only hand anomaly mask"),
        }
    elif mask_preset in {"accepted_v5_hand_skin", "v5_hand_skin"}:
        base_masks = {
            "apple": (1020, 380, 1430, 680, "apple object mask"),
            "cup": (700, 595, 1135, 900, "cup object mask"),
            "hand": (1085, 520, 1665, 850, "skin/finger hand anomaly mask"),
        }
    elif mask_preset in {"accepted_v5_tight_hand", "v5_tight_hand"}:
        base_masks = {
            "apple": (1020, 380, 1430, 680, "apple object mask"),
            "cup": (700, 595, 1135, 900, "cup object mask"),
            # Keep the repair target away from the apple body and table surface.
            # The broader accepted_v5 hand mask is useful for stress testing, but
            # it can invite large-object hallucinations during anatomy repair.
            "hand": (1385, 455, 1710, 825, "tight hand anomaly mask"),
        }
    elif mask_preset in {"accepted_v5", "apple_v5", "v5"}:
        base_masks: dict[str, tuple[int, int, int, int, str]] = {
            "apple": (1020, 380, 1430, 680, "apple object mask"),
            "cup": (700, 595, 1135, 900, "cup object mask"),
            "hand": (1100, 500, 1650, 900, "hand anomaly mask"),
        }
    elif mask_preset in {"netayume_v1_objects", "netayume_object_v1"}:
        base_width, base_height = IMAGE_WIDTH, IMAGE_HEIGHT
        base_masks = {
            "apple": (165, 530, 365, 705, "apple object tight mask"),
            "cup": (820, 525, 1010, 1000, "right blue cup tight mask"),
            "hand": (330, 235, 695, 355, "table-edge hands diagnostic mask"),
        }
    elif mask_preset in {"netayume_v1_apple_replace", "netayume_apple_replace_v1"}:
        base_width, base_height = IMAGE_WIDTH, IMAGE_HEIGHT
        base_masks = {
            "apple": (130, 490, 405, 780, "apple replacement mask"),
            "cup": (820, 525, 1010, 1000, "right blue cup tight mask"),
            "hand": (330, 235, 695, 355, "table-edge hands diagnostic mask"),
        }
    else:
        base_masks = {
            "apple": (80, 620, 720, 1060, "apple object mask"),
            "cup": (1210, 570, 1900, 1060, "cup object mask"),
            "hand": (820, 300, 1840, 940, "hand anomaly mask"),
        }

    def _scale_box(x1: int, y1: int, x2: int, y2: int) -> tuple[int, int, int, int]:
        sx1 = round(x1 * IMAGE_WIDTH / base_width)
        sy1 = round(y1 * IMAGE_HEIGHT / base_height)
        sx2 = round(x2 * IMAGE_WIDTH / base_width)
        sy2 = round(y2 * IMAGE_HEIGHT / base_height)
        sx1 = max(0, min(IMAGE_WIDTH - 1, sx1))
        sy1 = max(0, min(IMAGE_HEIGHT - 1, sy1))
        sx2 = max(sx1 + 1, min(IMAGE_WIDTH, sx2))
        sy2 = max(sy1 + 1, min(IMAGE_HEIGHT, sy2))
        return sx1, sy1, sx2, sy2

    masks = {
        key: (*_scale_box(x1, y1, x2, y2), label)
        for key, (x1, y1, x2, y2, label) in base_masks.items()
    }
    paths = {"source": source_out, "source_metadata": metadata_path}
    use_skin_hand_mask = mask_preset in {"accepted_v5_hand_skin", "v5_hand_skin"}
    use_fingertip_hand_mask = mask_preset in {"accepted_v5_fingertips", "v5_fingertips"}
    for key, (x1, y1, x2, y2, label) in masks.items():
        mask = Image.new("L", (IMAGE_WIDTH, IMAGE_HEIGHT), 0)
        md = ImageDraw.Draw(mask)
        if key == "hand" and mask_preset in {"six_finger_wide_bg", "six_finger_repair_wide_bg"}:
            md.rounded_rectangle([670, 396, 820, 806], radius=20, fill=255)
        elif key == "hand" and use_fingertip_hand_mask:
            def box(px1: int, py1: int, px2: int, py2: int) -> list[int]:
                return list(_scale_box(px1, py1, px2, py2))

            md.ellipse(box(1115, 690, 1510, 875), fill=255)
            md.ellipse(box(1210, 610, 1535, 765), fill=255)
            md.ellipse(box(1450, 500, 1680, 675), fill=255)
            md.rounded_rectangle(box(1310, 560, 1580, 790), radius=26, fill=255)
        elif key == "hand" and use_skin_hand_mask:
            # Approximate the visible skin regions of the accepted_v5 source.
            # This avoids asking the model to hallucinate an entire rectangular
            # hand/sleeve/background block when only anatomy needs repair.
            def box(px1: int, py1: int, px2: int, py2: int) -> list[int]:
                return list(_scale_box(px1, py1, px2, py2))

            md.rounded_rectangle(box(1270, 520, 1645, 780), radius=36, fill=255)
            md.ellipse(box(1110, 650, 1510, 850), fill=255)
            md.rounded_rectangle(box(1180, 580, 1490, 725), radius=28, fill=255)
            md.rounded_rectangle(box(1450, 500, 1665, 665), radius=34, fill=255)
        elif key == "apple":
            md.ellipse([x1, y1, x2, y2], fill=255)
            leaf_box = [
                round(x1 + (x2 - x1) * 0.45),
                max(0, round(y1 - (y2 - y1) * 0.16)),
                round(x1 + (x2 - x1) * 0.86),
                round(y1 + (y2 - y1) * 0.18),
            ]
            md.ellipse(leaf_box, fill=255)
        else:
            md.rectangle([x1, y1, x2, y2], fill=255)
        mask_rgb = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), (0, 0, 0))
        mask_rgb.paste(Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), (255, 255, 255)), mask=mask)
        overlay = source.copy()
        od = ImageDraw.Draw(overlay, "RGBA")
        if key == "hand" and (use_skin_hand_mask or use_fingertip_hand_mask):
            od.bitmap((0, 0), mask, fill=(255, 0, 0, 80))
        od.rectangle([x1, y1, x2, y2], outline=(255, 0, 0, 255), width=8)
        label_top = max(0, y1 - 42)
        label_right = max(x1 + 1, min(IMAGE_WIDTH, x1 + 360))
        od.rectangle([x1, label_top, label_right, y1], fill=(255, 255, 255, 220))
        od.text((x1 + 8, max(0, y1 - 36)), label, fill=(190, 0, 0, 255), font=_font(24))
        mask_path = assets / f"mask_{key}_{RESOLUTION_LABEL}.png"
        overlay_path = assets / f"mask_{key}_overlay.png"
        mask_rgb.save(mask_path)
        overlay.save(overlay_path)
        paths[f"mask_{key}"] = mask_path
        paths[f"mask_{key}_overlay"] = overlay_path
    return paths


def api_fetch(page, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    last_error = ""
    for attempt in range(5):
        try:
            return page.evaluate(
                """async ({method, path, body}) => {
                  const csrf = (document.cookie.match(/(?:^|; )csrf_token=([^;]+)/) || [])[1] || "";
                  const headers = {"X-CSRF-Token": decodeURIComponent(csrf)};
                  const opts = {method, credentials: "same-origin", headers};
                  if (body !== null && body !== undefined) {
                    headers["Content-Type"] = "application/json";
                    opts.body = JSON.stringify(body);
                  }
                  const res = await fetch(path, opts);
                  const text = await res.text();
                  let parsed = {};
                  try { parsed = text ? JSON.parse(text) : {}; } catch (e) { parsed = {raw: text}; }
                  return {status: res.status, ok: res.ok, body: parsed, text};
                }""",
                {"method": method, "path": path, "body": body},
            )
        except Exception as exc:
            last_error = str(exc)
            if attempt < 4:
                time.sleep(1 + attempt)
    return {
        "status": 0,
        "ok": False,
        "body": {"ok": False, "error": last_error, "path": path, "method": method},
        "text": last_error,
    }


def login(page, base_url: str, username: str, password: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.evaluate("() => fetch('/api/csrf-token', {credentials: 'same-origin'}).catch(() => null)")
    result = api_fetch(page, "POST", "/api/login", {"username": username, "password": password})
    if result["status"] != 200 or not result["body"].get("ok"):
        raise RuntimeError(f"login failed for {username}: {result}")
    page.goto(base_url + "/", wait_until="domcontentloaded")


def open_ai_agent(page, base_url: str) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.wait_for_function(
        """() => (
          typeof switchModuleTab === "function"
          && typeof currentUser !== "undefined"
          && !!currentUser
          && typeof canAccessModule === "function"
          && canAccessModule("ai-agent")
          && document.querySelector("#ai-agent-input")
        )""",
        timeout=20_000,
    )
    page.evaluate(
        """() => {
          if (typeof syncSidebarMenuVisibility === "function") syncSidebarMenuVisibility();
          switchModuleTab("ai-agent");
        }"""
    )
    page.locator("#module-ai-agent.active").wait_for(state="visible", timeout=15_000)
    page.locator("#ai-agent-input").wait_for(state="visible", timeout=15_000)


def import_image(page, path: Path, filename: str) -> dict[str, Any]:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    result = page.evaluate(
        """async ({encoded, filename}) => {
          const csrf = (document.cookie.match(/(?:^|; )csrf_token=([^;]+)/) || [])[1] || "";
          const bytes = Uint8Array.from(atob(encoded), c => c.charCodeAt(0));
          const blob = new Blob([bytes], {type: "image/png"});
          const form = new FormData();
          form.append("image", blob, filename);
          const res = await fetch("/api/comfyui/import-uploaded-image", {
            method: "POST",
            credentials: "same-origin",
            headers: {"X-CSRF-Token": decodeURIComponent(csrf)},
            body: form,
          });
          const text = await res.text();
          let parsed = {};
          try { parsed = text ? JSON.parse(text) : {}; } catch (e) { parsed = {raw: text}; }
          return {status: res.status, ok: res.ok, body: parsed, text};
        }""",
        {"encoded": encoded, "filename": filename},
    )
    if result["status"] != 200 or not result["body"].get("ok"):
        raise RuntimeError(f"image import failed for {filename}: {result}")
    return result["body"]["image"]


def seed_context(page, source: dict[str, Any], mask: dict[str, Any] | None, case: dict[str, Any]) -> None:
    page.evaluate(
        """({source, mask, caseInfo}) => {
          AI_AGENT_STATE.messages = [];
          AI_AGENT_STATE.lastComfyuiJob = {
            job_id: "i2i-audit-source-context",
            status: "completed",
            progress: {percent: 100},
            result: {images: [{filename: source.filename, image_ref: source.image_ref, mime_type: source.mime_type || "image/png"}]},
          };
          AI_AGENT_STATE.lastComfyuiArgs = {
            prompt: caseInfo.source_prompt,
            width: caseInfo.width,
            height: caseInfo.height,
            steps: 8,
            batch_size: 1,
            confirm_billing: true,
          };
          AI_AGENT_STATE.messages.push({
            role: "assistant",
            content: `測試原圖 source image for ${caseInfo.case_id}: ${caseInfo.width}x${caseInfo.height}。請把這張當 source_image_ref。提示詞 metadata 是「${caseInfo.source_prompt}」，它不是圖上的可見文字，不要把提示詞文字畫進結果圖。`,
            images: [{image_ref: source.image_ref, cloud_file_id: source.cloud_file_id || "", storage_file_id: source.storage_file_id || "", filename: source.filename, mime_type: source.mime_type || "image/png"}],
          });
          if (mask) {
            AI_AGENT_STATE.messages.push({
              role: "assistant",
              content: `${caseInfo.mask_label || "inpaint mask"} for ${caseInfo.case_id}. 這張是遮罩圖，請把這張當 mask_image_ref，不要把它當 source。`,
              images: [{image_ref: mask.image_ref, cloud_file_id: mask.cloud_file_id || "", storage_file_id: mask.storage_file_id || "", filename: mask.filename, mime_type: mask.mime_type || "image/png"}],
            });
          }
          renderAiAgentThread();
        }""",
        {
            "source": source,
            "mask": mask,
            "caseInfo": {
                "case_id": case["case_id"],
                "mask_label": f"{case.get('mask_key') or ''} object mask".strip(),
            "source_prompt": case.get("source_prompt") or SOURCE_SCENE_PROMPT,
                "width": IMAGE_WIDTH,
                "height": IMAGE_HEIGHT,
            },
        },
    )


def ai_agent_preflight(page) -> dict[str, Any]:
    return page.evaluate(
        """async () => {
          if (typeof loadAiAgentStatus === "function") {
            await loadAiAgentStatus({force: true}).catch((err) => {
              window.__aiAgentPreflightError = String(err && err.message ? err.message : err);
            });
          }
          if (AI_AGENT_STATE.actor?.role === "super_admin" && typeof loadAiAgentWriteToolCatalog === "function") {
            await loadAiAgentWriteToolCatalog({force: true}).catch(() => undefined);
          }
          if (typeof aiAgentSelectedTextModel === "function") aiAgentSelectedTextModel();
          const select = document.querySelector("#ai-agent-model");
          const msg = document.querySelector("#ai-agent-msg");
          const sendBtn = document.querySelector("#ai-agent-send-btn");
          return {
            loaded: !!AI_AGENT_STATE.loaded,
            loading: !!AI_AGENT_STATE.loading,
            sending: !!AI_AGENT_STATE.sending,
            sendingTool: !!AI_AGENT_STATE.sendingTool,
            actor: AI_AGENT_STATE.actor || {},
            operation_mode: AI_AGENT_STATE.settings?.operation_mode || "",
            selected_model: select?.value || "",
            selectable_models: typeof aiAgentSelectableModels === "function" ? aiAgentSelectableModels() : [],
            model_ids: AI_AGENT_STATE.modelIds || [],
            allowed_models: AI_AGENT_STATE.settings?.allowed_models || "",
            write_tools: (AI_AGENT_STATE.writeToolCatalog || []).map((tool) => tool.name).filter(Boolean),
            can_run_comfyui: typeof aiAgentCanRunWriteTool === "function" ? aiAgentCanRunWriteTool("write_comfyui_generate") : false,
            can_elevate_comfyui: typeof aiAgentCanRequestWriteElevation === "function" ? aiAgentCanRequestWriteElevation("write_comfyui_generate") : false,
            send_disabled: !!sendBtn?.disabled,
            message: msg?.textContent || "",
            preflight_error: window.__aiAgentPreflightError || "",
          };
        }"""
    )


def ensure_live_ai_agent_settings(page, *, model: str, api_base_url: str, comfyui_api_url: str) -> dict[str, Any]:
    before = api_fetch(page, "GET", "/api/admin/settings")
    settings = (before.get("body") or {}).get("settings") if isinstance(before.get("body"), dict) else {}
    allowed = [
        item.strip()
        for item in str((settings or {}).get("ai_agent_allowed_models") or "").split(",")
        if item.strip() and item.strip() != "qwen3-vl:235b-instruct-cloud"
    ]
    if model not in allowed:
        allowed.append(model)
    payload = {
        "ai_agent_provider": "openai_compatible",
        "ai_agent_api_base_url": api_base_url.rstrip("/"),
        "ai_agent_model": model,
        "ai_agent_allowed_models": ",".join(allowed),
        "ai_agent_operation_mode": "write",
        "ai_agent_allowed_tools": "write_comfyui_generate",
        "ai_agent_allow_image_input": True,
        "comfyui_connection_mode": "remote",
        "comfyui_remote_api_url": comfyui_api_url.rstrip("/"),
    }
    after = api_fetch(page, "PUT", "/api/admin/settings", payload)
    return {"before": before, "request": payload, "after": after}


def send_ai_agent_message(page, text: str, *, timeout_ms: int = 180_000) -> dict[str, Any]:
    page.fill("#ai-agent-input", text)
    return page.evaluate(
        """async ({timeoutMs}) => {
          const snapshot = () => ({
            message_count: document.querySelectorAll("#ai-agent-thread .ai-agent-message").length,
            msg: document.querySelector("#ai-agent-msg")?.textContent || "",
            selected_model: document.querySelector("#ai-agent-model")?.value || "",
            send_disabled: !!document.querySelector("#ai-agent-send-btn")?.disabled,
            input_value_length: (document.querySelector("#ai-agent-input")?.value || "").length,
            state: {
              loaded: !!AI_AGENT_STATE.loaded,
              loading: !!AI_AGENT_STATE.loading,
              sending: !!AI_AGENT_STATE.sending,
              sendingTool: !!AI_AGENT_STATE.sendingTool,
              readonlyLoading: !!AI_AGENT_STATE.readonlyLoading,
              writeToolLoading: !!AI_AGENT_STATE.writeToolLoading,
              message_count: (AI_AGENT_STATE.messages || []).length,
              recent_image_ref_count: typeof aiAgentRecentImageRefs === "function" ? aiAgentRecentImageRefs(12).length : null,
              planner_context_chars: (() => {
                try {
                  return typeof aiAgentPlannerContext === "function"
                    ? JSON.stringify(aiAgentPlannerContext({mode: "text", hasImage: false})).length
                    : null;
                } catch (err) {
                  return `error:${String(err && err.message ? err.message : err).slice(0, 180)}`;
                }
              })(),
            },
          });
          const before = {
            message_count: document.querySelectorAll("#ai-agent-thread .ai-agent-message").length,
            msg: document.querySelector("#ai-agent-msg")?.textContent || "",
            selected_model: document.querySelector("#ai-agent-model")?.value || "",
            send_disabled: !!document.querySelector("#ai-agent-send-btn")?.disabled,
          };
          const sendTask = (async () => {
            if (typeof sendAiAgentMessage !== "function") {
              throw new Error("sendAiAgentMessage is not available");
            }
            await sendAiAgentMessage();
            return {
              ok: true,
              before,
              after: {
                message_count: document.querySelectorAll("#ai-agent-thread .ai-agent-message").length,
                msg: document.querySelector("#ai-agent-msg")?.textContent || "",
                selected_model: document.querySelector("#ai-agent-model")?.value || "",
                send_disabled: !!document.querySelector("#ai-agent-send-btn")?.disabled,
              },
            };
          })();
          const timeoutTask = new Promise((resolve) => {
            setTimeout(() => resolve({
              ok: false,
              error: `sendAiAgentMessage timeout after ${Math.round(timeoutMs / 1000)}s`,
              before,
              after: snapshot(),
            }), timeoutMs);
          });
          try {
            return await Promise.race([sendTask, timeoutTask]);
          } catch (err) {
            return {
              ok: false,
              error: String(err && err.message ? err.message : err),
              before,
              after: snapshot(),
            };
          }
        }"""
        ,
        {"timeoutMs": timeout_ms},
    )


def thread_text(page) -> str:
    try:
        return page.locator("#ai-agent-thread").inner_text(timeout=5_000)
    except Exception:
        return ""


def thread_messages(page) -> list[str]:
    try:
        return page.evaluate(
            """() => Array.from(document.querySelectorAll('#ai-agent-thread .ai-agent-message'))
              .map((el) => (el.innerText || '').trim()).filter(Boolean)"""
        )
    except Exception:
        return []


def anomaly_metrics(messages: list[str]) -> dict[str, Any]:
    repeated_adjacent = 0
    repeated_total = 0
    seen: dict[str, int] = {}
    progress: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    last_percent: dict[str, int] = {}
    for idx, message in enumerate(messages):
        normalized = re.sub(r"\s+", " ", message).strip()
        if idx > 0 and normalized == re.sub(r"\s+", " ", messages[idx - 1]).strip():
            repeated_adjacent += 1
        seen[normalized] = seen.get(normalized, 0) + 1
        if seen[normalized] == 2:
            repeated_total += 1
        for job_id in re.findall(r"Job ID[:：]\s*([A-Za-z0-9_-]+)", message):
            percent_match = re.search(r"進度[:：]\s*(\d+)%", message)
            if percent_match:
                percent = int(percent_match.group(1))
                previous = last_percent.get(job_id)
                if previous is not None and percent < previous:
                    regressions.append({"job_id": job_id, "previous": previous, "current": percent})
                last_percent[job_id] = percent
                progress.append({"job_id": job_id, "percent": percent})
    return {
        "message_count": len(messages),
        "repeated_adjacent": repeated_adjacent,
        "repeated_total": repeated_total,
        "progress_snapshots": progress,
        "progress_regressions": regressions,
    }


def latest_job_id_from_text(text: str) -> str:
    matches = re.findall(r"Job ID[:：]\s*([A-Za-z0-9_-]+)", text or "")
    return matches[-1] if matches else ""


def extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL | re.IGNORECASE)
    candidates = [fenced.group(1)] if fenced else []
    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        candidates.append(raw[first:last + 1])
    candidates.append(raw)
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def wait_job(page, job_id: str, timeout_seconds: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.time() + timeout_seconds
    polls: list[dict[str, Any]] = []
    last_job: dict[str, Any] = {}
    completed_phase_seen_at: float | None = None
    while time.time() < deadline:
        result = api_fetch(page, "GET", f"/api/comfyui/jobs/{job_id}")
        last_job = (result.get("body") or {}).get("job") or {}
        progress = last_job.get("progress") if isinstance(last_job.get("progress"), dict) else {}
        polls.append({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "http_status": result.get("status"),
            "ok": result.get("ok"),
            "job_status": last_job.get("status"),
            "phase": progress.get("phase"),
            "percent": progress.get("percent"),
            "detail": progress.get("detail") or progress.get("error_message") or last_job.get("error"),
        })
        status = str(last_job.get("status") or "").lower()
        phase = str(progress.get("phase") or "").lower()
        if status in {"completed", "error", "failed", "cancelled"} or phase in {"error", "failed", "cancelled"}:
            return last_job, polls
        if phase in {"completed", "succeeded"} or progress.get("completed") is True:
            if completed_phase_seen_at is None:
                completed_phase_seen_at = time.time()
            elif time.time() - completed_phase_seen_at >= 45:
                last_job["status"] = "completed_pending_result"
                return last_job, polls
        time.sleep(5)
    last_job["timed_out"] = True
    return last_job, polls


def save_preview(page, image_ref: dict[str, Any], out_path: Path) -> dict[str, Any]:
    result = api_fetch(page, "POST", "/api/comfyui/image-preview", {"image_ref": image_ref})
    if result["status"] != 200 or not result["body"].get("ok"):
        return {"ok": False, "error": result}
    image = result["body"]["image"]
    data_url = image.get("data_url") or ""
    if "," not in data_url:
        return {"ok": False, "error": "missing data_url"}
    out_path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
    return {
        "ok": True,
        "path": str(out_path),
        "mime_type": image.get("mime_type"),
        "size_bytes": image.get("size_bytes"),
    }


def save_preview_with_retry(
    page,
    image_ref: dict[str, Any],
    out_path: Path,
    *,
    attempts: int = 5,
    default_sleep_seconds: float = 2.0,
) -> dict[str, Any]:
    errors: list[Any] = []
    for attempt in range(1, max(1, attempts) + 1):
        preview = save_preview(page, image_ref, out_path)
        if preview.get("ok"):
            if errors:
                preview["retry_errors"] = errors
            preview["attempts"] = attempt
            return preview
        errors.append(preview.get("error", preview))
        error = preview.get("error") if isinstance(preview, dict) else {}
        body = error.get("body") if isinstance(error, dict) and isinstance(error.get("body"), dict) else {}
        if attempt >= attempts:
            break
        retry_after = body.get("retry_after_seconds")
        try:
            sleep_seconds = float(retry_after)
        except (TypeError, ValueError):
            sleep_seconds = default_sleep_seconds
        time.sleep(max(0.25, min(10.0, sleep_seconds)))
    return {"ok": False, "error": errors[-1] if errors else "preview failed", "retry_errors": errors, "attempts": attempts}


def detect_visual_artifacts(image_path: str | Path) -> dict[str, Any]:
    path = Path(image_path)
    if not path.is_file():
        return {"checked": False, "error": "image not found"}
    image = Image.open(path).convert("RGB")
    width, height = image.size
    all_pixels = list(image.getdata())
    if not all_pixels:
        return {"checked": False, "error": "image has no pixels", "has_blocking_artifact": True}
    channel_extrema = image.getextrema()
    max_channel = max((pair[1] for pair in channel_extrema), default=0)
    min_channel = min((pair[0] for pair in channel_extrema), default=0)
    dynamic_range = max_channel - min_channel
    mean_luminance = sum((int(r) + int(g) + int(b)) / 3 for r, g, b in all_pixels) / len(all_pixels)
    blank_reason = ""
    if max_channel <= 2 and mean_luminance <= 1.5:
        blank_reason = "image is nearly all black"
    elif min_channel >= 253 and mean_luminance >= 253:
        blank_reason = "image is nearly all white"
    elif dynamic_range <= 2:
        blank_reason = "image is nearly a single flat color"
    tile_w = max(16, width // 48)
    tile_h = max(16, height // 27)
    cols = (width + tile_w - 1) // tile_w
    rows = (height + tile_h - 1) // tile_h
    gray_tiles: set[tuple[int, int]] = set()
    total_tiles = 0
    for row in range(rows):
        for col in range(cols):
            x1 = col * tile_w
            y1 = row * tile_h
            x2 = min(width, x1 + tile_w)
            y2 = min(height, y1 + tile_h)
            if x2 <= x1 or y2 <= y1:
                continue
            total_tiles += 1
            tile = image.crop((x1, y1, x2, y2))
            pixels = list(tile.getdata())
            if not pixels:
                continue
            gray_like = 0
            luminance_values = []
            for r, g, b in pixels:
                lum = (int(r) + int(g) + int(b)) / 3
                luminance_values.append(lum)
                if 95 <= lum <= 190 and max(abs(r - g), abs(r - b), abs(g - b)) <= 8:
                    gray_like += 1
            gray_ratio = gray_like / len(pixels)
            mean = sum(luminance_values) / len(luminance_values)
            variance = sum((value - mean) ** 2 for value in luminance_values) / len(luminance_values)
            if gray_ratio >= 0.88 and variance <= 24:
                gray_tiles.add((row, col))
    visited: set[tuple[int, int]] = set()
    largest_component = 0
    largest_component_fill_ratio = 0.0
    largest_component_edge_count = 0
    largest_component_luminance_range = 0.0
    largest_component_luminance_stddev = 0.0
    for tile in list(gray_tiles):
        if tile in visited:
            continue
        stack = [tile]
        visited.add(tile)
        component_tiles = []
        size = 0
        while stack:
            current = stack.pop()
            component_tiles.append(current)
            size += 1
            r, c = current
            for neighbor in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
                if neighbor in gray_tiles and neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        if size > largest_component:
            largest_component = size
            component_rows = [row for row, _ in component_tiles]
            component_cols = [col for _, col in component_tiles]
            min_row, max_row = min(component_rows), max(component_rows)
            min_col, max_col = min(component_cols), max(component_cols)
            bbox_area = max(1, (max_row - min_row + 1) * (max_col - min_col + 1))
            largest_component_fill_ratio = size / bbox_area
            largest_component_edge_count = sum((
                min_row == 0,
                min_col == 0,
                max_row == rows - 1,
                max_col == cols - 1,
            ))
            luminance_means = []
            for row, col in component_tiles:
                x1 = col * tile_w
                y1 = row * tile_h
                x2 = min(width, x1 + tile_w)
                y2 = min(height, y1 + tile_h)
                tile = image.crop((x1, y1, x2, y2))
                pixels = list(tile.getdata())
                if pixels:
                    luminance_means.append(sum((int(r) + int(g) + int(b)) / 3 for r, g, b in pixels) / len(pixels))
            if luminance_means:
                mean = sum(luminance_means) / len(luminance_means)
                largest_component_luminance_range = max(luminance_means) - min(luminance_means)
                largest_component_luminance_stddev = (
                    sum((value - mean) ** 2 for value in luminance_means) / len(luminance_means)
                ) ** 0.5
    component_ratio = largest_component / max(1, total_tiles)
    gray_tile_ratio = len(gray_tiles) / max(1, total_tiles)
    suspicious_uniform_gray_frame = (
        largest_component_edge_count >= 2
        and (
            largest_component_fill_ratio >= 0.85
            or component_ratio >= 0.25
            or gray_tile_ratio >= 0.35
        )
        and (
            largest_component_luminance_range <= 24
            or largest_component_luminance_stddev <= 8
        )
    )
    gray_blocking = (
        suspicious_uniform_gray_frame
        and (component_ratio >= 0.08 or gray_tile_ratio >= 0.16)
    )
    blocking = bool(blank_reason) or gray_blocking
    return {
        "checked": True,
        "image_size": [width, height],
        "mean_luminance": round(mean_luminance, 4),
        "dynamic_range": int(dynamic_range),
        "is_blank_or_flat": bool(blank_reason),
        "tile_size": [tile_w, tile_h],
        "gray_tile_count": len(gray_tiles),
        "total_tile_count": total_tiles,
        "gray_tile_ratio": round(gray_tile_ratio, 4),
        "largest_gray_component_tiles": largest_component,
        "largest_gray_component_ratio": round(component_ratio, 4),
        "largest_gray_component_fill_ratio": round(largest_component_fill_ratio, 4),
        "largest_gray_component_edge_count": largest_component_edge_count,
        "largest_gray_component_luminance_range": round(largest_component_luminance_range, 4),
        "largest_gray_component_luminance_stddev": round(largest_component_luminance_stddev, 4),
        "suspicious_uniform_gray_frame": suspicious_uniform_gray_frame,
        "has_blocking_artifact": blocking,
        "reason": blank_reason or ("large uniform gray frame" if gray_blocking else ""),
    }


def first_result_image(job: dict[str, Any]) -> dict[str, Any] | None:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    images = result.get("images") if isinstance(result.get("images"), list) else []
    if not images and isinstance(result.get("image"), dict):
        images = [result["image"]]
    for image in images:
        ref = image.get("image_ref") or image.get("file_ref")
        if isinstance(ref, dict) and ref.get("filename"):
            return {"image_ref": ref, "filename": ref.get("filename") or image.get("filename") or ""}
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    partial_outputs = progress.get("partial_outputs") if isinstance(progress.get("partial_outputs"), dict) else {}
    partial_images = partial_outputs.get("images") if isinstance(partial_outputs.get("images"), list) else []
    for image in partial_images:
        if isinstance(image, dict) and image.get("filename"):
            return {
                "image_ref": {
                    "filename": image.get("filename"),
                    "subfolder": image.get("subfolder") or "",
                    "type": image.get("type") or "output",
                },
                "filename": image.get("filename") or "",
            }
    return None


def write_report(out_dir: Path, report: dict[str, Any]) -> Path:
    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / "AI_AGENT_I2I_AUDIT_REPORT.md"
    lines = [
        "# AI Agent i2i 編輯實測報告",
        "",
        f"- 測試時間：{report['started_at']} ~ {report.get('finished_at') or '-'}",
        f"- Base URL：{report['base_url']}",
        f"- 固定基礎提示詞：`{BASE_PROMPT}`",
        f"- 原圖 scene prompt metadata：`{report.get('source_scene_prompt') or SOURCE_SCENE_PROMPT}`",
        "- 原圖可見文字：無；提示詞只存在於 metadata/對話/報告，不畫進 source image。",
        f"- 原圖來源類型：`{report.get('source_origin_type') or '-'}`",
        f"- 驗收規則：{report.get('delivery_acceptance_rule') or '正式通過必須使用本輪站內 t2i 實圖作為 source。'}",
        f"- 固定解析度：{report.get('fixed_resolution') or RESOLUTION_LABEL}",
        "- 固定品質目標：SDXL 等級，batch 1",
        f"- JSON 原始紀錄：`{json_path.name}`",
        "",
        "## 原圖與遮罩",
        "",
        f"![source]({report.get('source_image_rel') or f'assets/{SOURCE_IMAGE_NAME}'})",
        "",
    ]
    for key in ["apple", "cup", "hand"]:
        lines.extend([
            f"### {key} mask",
            f"![{key} mask overlay](assets/mask_{key}_overlay.png)",
            "",
        ])
    lines.extend([
        "## 案例結果",
        "",
        "| Case | 預期效果 | 狀態 | 達成率 | 耗時 | 使用模型 | 實際模式/參數 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ])
    rendered_cases = []
    if isinstance(report.get("source_generation"), dict):
        rendered_cases.append(report["source_generation"])
    rendered_cases.extend(report["cases"])
    for case in rendered_cases:
        args = case.get("write_arguments") or {}
        model = case.get("chat_model") or args.get("model") or args.get("checkpoint") or "-"
        params = (
            f"mode={args.get('generation_mode') or '-'}, "
            f"size={args.get('width') or '-'}x{args.get('height') or '-'}, "
            f"steps={args.get('steps') or '-'}, denoise={args.get('denoise_strength') or '-'}"
        )
        lines.append(
            "| {case_id} | {expected} | {status} | {score} | {elapsed}s | {model} | `{params}` |".format(
                case_id=case["case_id"],
                expected=case["expected"].replace("|", "/"),
                status=case.get("job_status") or case.get("status") or "-",
                score=case.get("manual_score", "待人工視覺判定"),
                elapsed=case.get("elapsed_seconds", "-"),
                model=str(model).replace("|", "/"),
                params=params.replace("|", "/"),
            )
        )
    lines.append("")
    for case in rendered_cases:
        lines.extend([
            f"## {case['case_id']} - {case['title']}",
            "",
            f"- 自然語言指令：{case['natural_language']}",
            f"- 預期效果：{case['expected']}",
            f"- Agent 規劃/回覆耗時：{case.get('planner_elapsed_seconds', '-') } 秒",
            f"- 端到端耗時：{case.get('elapsed_seconds', '-') } 秒",
            f"- 使用模型：{case.get('chat_model') or '-'}",
            f"- usage/token：`{json.dumps(case.get('usage') or {}, ensure_ascii=False)}`",
            f"- tokens/s：{case.get('tokens_per_second') if case.get('tokens_per_second') is not None else '後端未提供足夠 token/duration 資訊'}",
            f"- Job ID：{case.get('job_id') or '-'}",
            f"- Job 狀態：{case.get('job_status') or '-'}",
            f"- 實際 write-tool：`{case.get('write_tool') or '-'}`",
            f"- 實際參數：",
            "",
            "```json",
            json.dumps(case.get("write_arguments") or {}, ensure_ascii=False, indent=2),
            "```",
            "",
        ])
        if case.get("mask_overlay_rel"):
            lines.extend([f"遮罩參考：![mask]({case['mask_overlay_rel']})", ""])
        if case.get("reference_image_rel"):
            lines.extend([f"Reference 圖：![reference]({case['reference_image_rel']})", ""])
        if case.get("result_image_rel"):
            lines.extend([f"結果圖：![result]({case['result_image_rel']})", ""])
        else:
            lines.extend([f"結果圖：未取得。錯誤：`{json.dumps(case.get('error') or {}, ensure_ascii=False)}`", ""])
        lines.extend([
            f"- 自動異常指標：`{json.dumps(case.get('thread_anomaly_metrics') or {}, ensure_ascii=False)}`",
            f"- 視覺 artifact 檢測：`{json.dumps(case.get('visual_artifacts') or {}, ensure_ascii=False)}`",
            f"- 我的視覺判斷：{case.get('manual_judgement') or '待人工視覺判定'}",
            "",
        ])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    parser.add_argument("--username", default="root")
    parser.add_argument("--root-password", default="root")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--job-timeout-seconds", type=int, default=1800)
    parser.add_argument("--model", default=DEFAULT_AI_AGENT_MODEL)
    parser.add_argument("--api-base-url", default=DEFAULT_AI_AGENT_API_BASE_URL)
    parser.add_argument("--comfyui-api-url", default=DEFAULT_COMFYUI_API_URL)
    parser.add_argument("--stop-after-source", action="store_true")
    parser.add_argument("--source-profile", choices=["object", "person-i2i"], default="object")
    parser.add_argument("--source-official-workflow-id", default="")
    parser.add_argument("--source-instruction-suffix", default="")
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"/tmp/hackme_ai_agent_real_i2i_audit_{stamp}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    result_dir = out_dir / "results"
    result_dir.mkdir(parents=True, exist_ok=True)
    asset_dir = out_dir / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)

    source_scene_prompt = PERSON_I2I_SOURCE_SCENE_PROMPT if args.source_profile == "person-i2i" else SOURCE_SCENE_PROMPT
    source_case = PERSON_I2I_SOURCE_GENERATION_CASE if args.source_profile == "person-i2i" else SOURCE_GENERATION_CASE

    report: dict[str, Any] = {
        "ok": False,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": args.base_url.rstrip("/"),
        "login_username": args.username,
        "base_prompt": BASE_PROMPT,
        "source_scene_prompt": source_scene_prompt,
        "source_profile": args.source_profile,
        "source_origin_type": "real_t2i_source",
        "delivery_acceptance_rule": (
            "Only i2i cases that use this live front-end AI Agent txt2img result as source "
            "may be treated as delivery-pass candidates. Synthetic fixtures and preexisting "
            "images are diagnostic evidence only."
        ),
        "source_image_visible_prompt_text": False,
        "fixed_resolution": RESOLUTION_LABEL,
        "fixed_quality": "SDXL grade, batch 1",
        "source_official_workflow_id": args.source_official_workflow_id,
        "source_instruction_suffix": args.source_instruction_suffix,
        "cases": [],
        "browser_errors": [],
    }

    request_starts: dict[int, float] = {}
    chat_events: list[dict[str, Any]] = []
    write_events: list[dict[str, Any]] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1000})
        page = ctx.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        page.on("pageerror", lambda exc: report["browser_errors"].append(str(exc)))
        page.on("console", lambda msg: report["browser_errors"].append(msg.text) if msg.type == "error" else None)

        def on_request(request):
            if "/api/ai-agent/chat" in request.url or "/api/ai-agent/write-tools/execute" in request.url:
                request_starts[id(request)] = time.perf_counter()

        def on_response(response):
            url = response.url
            if "/api/ai-agent/chat" not in url and "/api/ai-agent/write-tools/execute" not in url:
                return
            elapsed = None
            started = request_starts.pop(id(response.request), None)
            if started is not None:
                elapsed = round(time.perf_counter() - started, 3)
            try:
                request_payload = response.request.post_data_json or {}
            except Exception:
                request_payload = {}
            try:
                payload = response.json()
            except Exception as exc:
                payload = {"parse_error": str(exc)}
            record = {
                "status": response.status,
                "elapsed_seconds": elapsed,
                "request": request_payload,
                "response": payload,
            }
            if "/api/ai-agent/chat" in url:
                chat_events.append(record)
            else:
                write_events.append(record)

        page.on("request", on_request)
        page.on("response", on_response)

        login(page, report["base_url"], args.username, args.root_password)
        report["settings_update"] = ensure_live_ai_agent_settings(
            page,
            model=args.model,
            api_base_url=args.api_base_url,
            comfyui_api_url=args.comfyui_api_url,
        )
        open_ai_agent(page, report["base_url"])

        report["ai_agent_status"] = api_fetch(page, "GET", "/api/ai-agent/status").get("body")
        report["ai_agent_models"] = api_fetch(page, "GET", "/api/ai-agent/models").get("body")
        report["comfyui_status"] = api_fetch(page, "GET", "/api/comfyui/status").get("body")
        report["comfyui_models"] = api_fetch(page, "GET", "/api/comfyui/models").get("body")

        before_chat = len(chat_events)
        before_writes = len(write_events)
        source_start = time.perf_counter()
        page.evaluate(
            """() => {
              AI_AGENT_STATE.messages = [];
              AI_AGENT_STATE.lastComfyuiJob = null;
              AI_AGENT_STATE.lastComfyuiArgs = null;
              renderAiAgentThread();
            }"""
        )
        source_preflight = ai_agent_preflight(page)
        source_natural_language = source_case["natural_language"]
        if args.source_official_workflow_id:
            source_natural_language += (
                f" 請明確使用 official_workflow_id={args.source_official_workflow_id}，"
                "不要改回 SDXL 預設 workflow。"
            )
        if args.source_instruction_suffix:
            source_natural_language += f" {args.source_instruction_suffix}"
        source_send_result = send_ai_agent_message(page, source_natural_language)
        deadline = time.time() + 20
        while time.time() < deadline and len(write_events) == before_writes:
            time.sleep(1)
        source_writes = write_events[before_writes:]
        source_chats = chat_events[before_chat:]
        source_write = source_writes[-1] if source_writes else {}
        source_write_response = source_write.get("response") if isinstance(source_write.get("response"), dict) else {}
        source_write_request = source_write.get("request") if isinstance(source_write.get("request"), dict) else {}
        source_write_args = source_write_request.get("arguments") if isinstance(source_write_request.get("arguments"), dict) else {}
        source_job_id = ""
        source_result_payload = source_write_response.get("result") if isinstance(source_write_response.get("result"), dict) else {}
        source_job_payload = source_result_payload.get("job") if isinstance(source_result_payload.get("job"), dict) else {}
        if source_job_payload:
            source_job_id = str(source_job_payload.get("job_id") or "")
        if not source_job_id:
            source_job_id = latest_job_id_from_text(thread_text(page))
        source_job: dict[str, Any] = {}
        source_polls: list[dict[str, Any]] = []
        source_preview: dict[str, Any] = {}
        source_image: dict[str, Any] | None = None
        if source_job_id:
            source_job, source_polls = wait_job(page, source_job_id, args.job_timeout_seconds)
            source_image = first_result_image(source_job)
            if source_image:
                source_preview = save_preview(page, source_image["image_ref"], out_dir / "assets" / SOURCE_IMAGE_NAME)
        source_visual_artifacts = detect_visual_artifacts(source_preview["path"]) if source_preview.get("ok") else {}
        source_raw_status = str(source_job.get("status") or "")
        source_progress = source_job.get("progress") if isinstance(source_job.get("progress"), dict) else {}
        source_phase = str(source_progress.get("phase") or "").lower()
        source_effective_status = source_raw_status
        if (
            source_raw_status in {"running", "completed_pending_result"}
            and (source_progress.get("completed") is True or source_phase in {"completed", "succeeded"})
            and source_preview.get("ok")
        ):
            source_effective_status = "completed"
        source_messages = thread_messages(page)
        source_chat_response = source_chats[-1]["response"] if source_chats and isinstance(source_chats[-1].get("response"), dict) else {}
        source_planner_plan = extract_json_object(
            (source_chat_response.get("message") or {}).get("content")
            if isinstance(source_chat_response.get("message"), dict)
            else ""
        )
        if not source_write_args and isinstance(source_planner_plan.get("args"), dict):
            source_write_args = source_planner_plan["args"]
        if not source_write_request.get("tool") and source_planner_plan.get("tool"):
            source_write_request["tool"] = source_planner_plan.get("tool")
        source_usage = source_chat_response.get("usage") if isinstance(source_chat_response.get("usage"), dict) else {}
        source_completion_tokens = source_usage.get("completion_tokens") or source_usage.get("output_tokens") or source_usage.get("eval_count")
        source_planner_elapsed = source_chats[-1].get("elapsed_seconds") if source_chats else None
        source_tokens_per_second = None
        if source_completion_tokens and source_planner_elapsed:
            try:
                source_tokens_per_second = round(float(source_completion_tokens) / float(source_planner_elapsed), 3)
            except Exception:
                source_tokens_per_second = None
        source_case_report = {
            "case_id": source_case["case_id"],
            "title": source_case["title"],
            "source_origin_type": "real_t2i_source",
            "delivery_gate": "required",
            "natural_language": source_natural_language,
            "expected": source_case["expected"],
            "base_prompt": BASE_PROMPT,
            "status": "submitted" if source_job_id else "not_submitted",
            "elapsed_seconds": round(time.perf_counter() - source_start, 3),
            "planner_elapsed_seconds": source_planner_elapsed,
            "chat_model": source_chat_response.get("model") or "",
            "usage": source_usage,
            "tokens_per_second": source_tokens_per_second,
            "write_tool": source_write_request.get("tool") or "",
            "write_arguments": source_write_args,
            "write_response": source_write_response,
            "job_id": source_job_id,
            "job_status": source_effective_status,
            "raw_job_status": source_raw_status,
            "job": source_job,
            "job_polls": source_polls,
            "result_preview": source_preview,
            "result_image_rel": f"assets/{SOURCE_IMAGE_NAME}" if source_preview.get("ok") else "",
            "mask_overlay_rel": "",
            "chat_events": source_chats,
            "write_events": source_writes,
            "thread_tail": thread_text(page)[-6000:],
            "thread_anomaly_metrics": anomaly_metrics(source_messages),
            "visual_artifacts": source_visual_artifacts,
            "preflight": source_preflight,
            "send_result": source_send_result,
            "manual_score": "0%" if source_visual_artifacts.get("has_blocking_artifact") else "待人工視覺判定",
            "manual_judgement": "自動檢測到大面積灰色 artifact；原圖不合格。" if source_visual_artifacts.get("has_blocking_artifact") else "待人工視覺判定",
        }
        if not source_job_id:
            source_case_report["error"] = {
                "reason": "agent did not submit write tool for txt2img source or no Job ID was recoverable from the UI thread",
                "thread_tail": source_case_report["thread_tail"],
                "write_response": source_write_response,
            }
        elif str(source_effective_status or "").lower() != "completed":
            source_case_report["error"] = {
                "reason": "txt2img source job did not complete",
                "job_status": source_raw_status,
                "job_progress": source_job.get("progress"),
            }
        elif not source_preview.get("ok"):
            source_case_report["error"] = {"reason": "txt2img source preview failed", "preview": source_preview}
        report["source_generation"] = source_case_report
        if not source_preview.get("ok") or not source_image:
            report["cases"].append(source_case_report)
            write_report(out_dir, {**report, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            raise RuntimeError(f"txt2img source generation failed: {source_case_report.get('error')}")
        if args.stop_after_source:
            report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            report["ok"] = (
                str(source_case_report.get("job_status") or "").lower() == "completed"
                and bool(source_case_report.get("result_preview", {}).get("ok"))
                and not bool((source_case_report.get("visual_artifacts") or {}).get("has_blocking_artifact"))
                and not report["browser_errors"]
            )
            screenshot = out_dir / "ai_agent_source_generation.png"
            page.screenshot(path=str(screenshot), full_page=True)
            report["screenshot"] = str(screenshot)
            write_report(out_dir, report)
            browser.close()
            print(json.dumps({"ok": report["ok"], "report": str(out_dir / "AI_AGENT_I2I_AUDIT_REPORT.md"), "out_dir": str(out_dir)}, ensure_ascii=False, indent=2))
            return 0 if report["ok"] else 1

        asset_paths = make_mask_assets_for_source(out_dir, Path(source_preview["path"]))
        imported = {
            "source": {
                **source_image["image_ref"],
                "image_ref": source_image["image_ref"],
                "filename": source_image.get("filename") or source_image["image_ref"].get("filename"),
                "mime_type": "image/png",
            },
            "apple": import_image(page, asset_paths["mask_apple"], f"agent_i2i_mask_apple_{RESOLUTION_LABEL}.png"),
            "cup": import_image(page, asset_paths["mask_cup"], f"agent_i2i_mask_cup_{RESOLUTION_LABEL}.png"),
            "hand": import_image(page, asset_paths["mask_hand"], f"agent_i2i_mask_hand_{RESOLUTION_LABEL}.png"),
        }
        report["imported_images"] = imported

        for case in CASES:
            before_chat = len(chat_events)
            before_writes = len(write_events)
            mask_key = case.get("mask_key")
            mask = imported.get(mask_key) if mask_key else None
            seed_context(page, imported["source"], mask, case)
            preflight = ai_agent_preflight(page)
            start = time.perf_counter()
            send_result = send_ai_agent_message(page, case["natural_language"])

            deadline = time.time() + 15
            while time.time() < deadline and len(write_events) == before_writes:
                if "需要補充" in thread_text(page) or "未送出" in thread_text(page):
                    break
                time.sleep(1)

            case_writes = write_events[before_writes:]
            case_chats = chat_events[before_chat:]
            write = case_writes[-1] if case_writes else {}
            write_response = write.get("response") if isinstance(write.get("response"), dict) else {}
            write_request = write.get("request") if isinstance(write.get("request"), dict) else {}
            write_args = write_request.get("arguments") if isinstance(write_request.get("arguments"), dict) else {}
            tool = write_request.get("tool") or ""

            job_id = ""
            result_payload = write_response.get("result") if isinstance(write_response.get("result"), dict) else {}
            job_payload = result_payload.get("job") if isinstance(result_payload.get("job"), dict) else {}
            if job_payload:
                job_id = str(job_payload.get("job_id") or "")
            if not job_id:
                job_id = latest_job_id_from_text(thread_text(page))
            job: dict[str, Any] = {}
            polls: list[dict[str, Any]] = []
            preview: dict[str, Any] = {}
            if job_id:
                job, polls = wait_job(page, job_id, args.job_timeout_seconds)
                image = first_result_image(job)
                if image:
                    preview_path = result_dir / f"{case['case_id']}_result.png"
                    preview = save_preview(page, image["image_ref"], preview_path)
            visual_artifacts = detect_visual_artifacts(preview["path"]) if preview.get("ok") else {}
            write_failed = bool(
                case_writes
                and isinstance(write_response, dict)
                and write_response.get("ok") is False
            )
            write_failure_stage = ""
            write_result_payload = (
                write_response.get("result")
                if isinstance(write_response.get("result"), dict)
                else {}
            )
            if write_failed:
                write_failure_stage = str(
                    write_result_payload.get("stage")
                    or write_response.get("stage")
                    or "write_tool_failed"
                )
            raw_job_status = str(job.get("status") or "")
            progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
            progress_phase = str(progress.get("phase") or "").lower()
            effective_job_status = raw_job_status
            if (
                raw_job_status in {"running", "completed_pending_result"}
                and (progress.get("completed") is True or progress_phase in {"completed", "succeeded"})
                and preview.get("ok")
            ):
                effective_job_status = "completed"

            time.sleep(2)
            messages = thread_messages(page)
            chat_response = case_chats[-1]["response"] if case_chats and isinstance(case_chats[-1].get("response"), dict) else {}
            usage = chat_response.get("usage") if isinstance(chat_response.get("usage"), dict) else {}
            completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or usage.get("eval_count")
            planner_elapsed = case_chats[-1].get("elapsed_seconds") if case_chats else None
            tokens_per_second = None
            if completion_tokens and planner_elapsed:
                try:
                    tokens_per_second = round(float(completion_tokens) / float(planner_elapsed), 3)
                except Exception:
                    tokens_per_second = None

            case_report = {
                "case_id": case["case_id"],
                "title": case["title"],
                "natural_language": case["natural_language"],
                "expected": case["expected"],
                "base_prompt": BASE_PROMPT,
                "status": "submitted" if job_id else ("tool_failed" if write_failed else "not_submitted"),
                "elapsed_seconds": round(time.perf_counter() - start, 3),
                "planner_elapsed_seconds": planner_elapsed,
                "chat_model": chat_response.get("model") or "",
                "usage": usage,
                "tokens_per_second": tokens_per_second,
                "write_tool": tool,
                "write_arguments": write_args,
                "write_response": write_response,
                "job_id": job_id,
                "job_status": effective_job_status or ("tool_failed" if write_failed else ""),
                "raw_job_status": raw_job_status,
                "job": job,
                "job_polls": polls,
                "result_preview": preview,
                "result_image_rel": str(Path(preview.get("path", "")).relative_to(out_dir)) if preview.get("ok") else "",
                "mask_overlay_rel": f"assets/mask_{mask_key}_overlay.png" if mask_key else "",
                "chat_events": case_chats,
                "write_events": case_writes,
                "thread_tail": thread_text(page)[-6000:],
                "thread_anomaly_metrics": anomaly_metrics(messages),
                "visual_artifacts": visual_artifacts,
                "preflight": preflight,
                "send_result": send_result,
                "manual_score": (
                    "0% / BLOCKED"
                    if write_failure_stage == "missing_workflow_dependency"
                    else ("0%" if visual_artifacts.get("has_blocking_artifact") else "待人工視覺判定")
                ),
                "manual_judgement": (
                    "Agent 已正確選擇並呼叫 write_comfyui_generate，但官方 workflow 依賴缺失，後端以 409 阻止送出。"
                    if write_failure_stage == "missing_workflow_dependency"
                    else (
                        "自動檢測到大面積灰色 artifact；結果不合格。"
                        if visual_artifacts.get("has_blocking_artifact")
                        else "待人工視覺判定"
                    )
                ),
            }
            if not job_id:
                if write_failed:
                    case_report["error"] = {
                        "reason": write_failure_stage,
                        "thread_tail": case_report["thread_tail"],
                        "write_response": write_response,
                    }
                else:
                    case_report["error"] = {
                        "reason": "agent did not submit write tool",
                        "thread_tail": case_report["thread_tail"],
                        "write_response": write_response,
                    }
            elif str(effective_job_status or "").lower() != "completed":
                case_report["error"] = {
                    "reason": "job did not complete",
                    "job_status": raw_job_status,
                    "job_progress": job.get("progress"),
                }
            elif not preview.get("ok"):
                case_report["error"] = {"reason": "result image preview failed", "preview": preview}
            elif visual_artifacts.get("has_blocking_artifact"):
                case_report["error"] = {"reason": "blocking visual artifact detected", "visual_artifacts": visual_artifacts}
            report["cases"].append(case_report)
            write_report(out_dir, {**report, "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})

        screenshot = out_dir / "ai_agent_i2i_audit_final.png"
        page.screenshot(path=str(screenshot), full_page=True)
        report["screenshot"] = str(screenshot)
        browser.close()

    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["ok"] = all(
        case.get("job_status") == "completed"
        and case.get("result_preview", {}).get("ok")
        and not (case.get("visual_artifacts") or {}).get("has_blocking_artifact")
        for case in report["cases"]
    ) and not report["browser_errors"]
    md_path = write_report(out_dir, report)
    print(json.dumps({"ok": report["ok"], "report": str(md_path), "out_dir": str(out_dir)}, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
