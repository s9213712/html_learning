#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Any
import shutil

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

from ai_agent_real_i2i_edit_audit import (
    BASE_PROMPT,
    CASES,
    IMAGE_HEIGHT,
    IMAGE_WIDTH,
    SOURCE_IMAGE_NAME,
    SOURCE_SCENE_PROMPT,
    ai_agent_preflight,
    anomaly_metrics,
    api_fetch,
    detect_visual_artifacts,
    ensure_live_ai_agent_settings,
    extract_json_object,
    first_result_image,
    import_image,
    latest_job_id_from_text,
    login,
    make_six_finger_repair_fixture,
    make_mask_assets_for_source,
    open_ai_agent,
    save_preview_with_retry,
    seed_context,
    send_ai_agent_message,
    thread_messages,
    thread_text,
    wait_job,
    write_report,
)

VISUAL_SCORING_POLICY = {
    "hard_fail_rule": (
        "Any hard visual defect makes the case fail regardless of prompt achievement percentage. "
        "Hard defects include six or extra fingers, missing required fingers, impossible limb intersections, "
        "body/clothing/object penetration, severe anatomy collapse, required body parts cropped out, "
        "blank/black/gray artifact frames, unreadable subject, or a different main subject."
    ),
    "score_rule": (
        "If no hard defect is present, score by prompt achievement rate: achieved_items / total_prompt_items. "
        "Use the percentage as the case score and list missing or partial items."
    ),
    "pass_threshold": "No hard defect and prompt achievement rate >= 80%.",
}


def _case_scoring_items(case: dict[str, Any]) -> list[str]:
    items = case.get("scoring_items")
    if isinstance(items, list):
        return [str(item) for item in items if str(item).strip()]
    expected = str(case.get("expected") or "")
    return [part.strip(" ；;。") for part in re.split(r"[；;。]", expected) if part.strip()]

QWEN_EDIT_CASES: list[dict[str, Any]] = [
    {
        "case_id": "11_qwen_replace_apple_with_plant",
        "artifact_slug": "replace_apple_with_plant",
        "title": "Qwen edit: apple to plant",
        "mask_key": None,
        "expected": "以 Qwen Image Edit 2509 把可見紅蘋果改成小盆栽；人物、杯子、背景應盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用我剛剛提供的測試原圖。"
            "把畫面中可見的紅蘋果替換成一個小盆栽，保持人物臉、藍色杯子、桌面和背景一致。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "12_qwen_remove_cup",
        "artifact_slug": "delete_cup",
        "title": "Qwen edit: remove cup",
        "mask_key": None,
        "expected": "藍色杯子被移除並自然補成桌面；人物和蘋果應保留。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用測試原圖。"
            "移除桌上的藍色杯子，補成自然木桌，不要改人物臉、蘋果或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "13_qwen_replace_cup_with_plush",
        "artifact_slug": "replace_cup_with_plush",
        "title": "Qwen edit: cup to plush",
        "mask_key": None,
        "expected": "藍色杯子被替換成白色小貓玩偶；人物、蘋果和背景應保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用測試原圖。"
            "把桌上的藍色杯子替換成白色小貓玩偶，保持人物臉、紅蘋果、桌面和背景一致。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "14_qwen_fix_hand",
        "artifact_slug": "repair_hand",
        "title": "Qwen edit: fix hand anomaly",
        "mask_key": None,
        "expected": "右側手部形狀更自然；臉部、杯子、蘋果和背景應保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用測試原圖。"
            "修正右側手部手指扭曲或多指異常，讓手看起來自然；不要改人物臉、藍色杯子、紅蘋果或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "14_qwen_replace_all_cups_with_one_plush",
        "artifact_slug": "replace_all_cups_with_one_plush",
        "title": "Qwen edit: all cups to one plush",
        "mask_key": None,
        "expected": "右側所有可見藍色杯子、杯緣與杯把都消失，改成單一白色小貓玩偶；人物、蘋果和背景應保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用測試原圖。"
            "原圖右側可能有多個可見藍色杯狀物，請把所有藍色杯子、杯緣和杯把都替換成單一白色小貓玩偶，"
            "不要留下任何藍色杯子殘影；保持人物臉、雙手、紅蘋果、桌面和背景一致。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "15_qwen_replace_upper_cup_remove_foreground_cup",
        "artifact_slug": "replace_upper_cup_delete_foreground_cup",
        "title": "Qwen edit: upper cup to plush, foreground cup removed",
        "mask_key": None,
        "expected": "右上主藍杯替換成單一白色小貓玩偶；右下裁切前景杯完全移除並補成空木桌；人物、蘋果和背景應保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用測試原圖。"
            "請做兩個不同處理：右上主藍杯替換成單一白色小貓玩偶；右下角裁切的前景藍杯、杯緣與杯把完全移除並補成空木桌，"
            "不要在右下角新增任何玩偶或新物件。保持人物臉、雙手、紅蘋果、桌面和背景一致。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "16_qwen_change_expression_surprised",
        "artifact_slug": "change_expression_surprised",
        "title": "Qwen edit: expression to surprised",
        "mask_key": None,
        "expected": "女孩表情變成驚訝，眼睛或嘴型有明顯變化；手、蘋果、杯子、桌面和背景應保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用測試原圖。"
            "只把女孩的表情改成驚訝表情，眼睛睜大、嘴巴微張；不要改髮型、衣服、手、紅蘋果、藍色杯子、桌面或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "17_qwen_change_clothing_red_hoodie",
        "artifact_slug": "change_red_hoodie",
        "title": "Qwen edit: visible clothing to red hoodie",
        "mask_key": None,
        "expected": "女孩可見肩膀與袖子變成紅色連帽衫風格；臉、頭髮、手、蘋果、杯子、桌面和背景應保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用測試原圖。"
            "只把女孩可見的衣服、肩膀和袖子改成紅色連帽衫風格，可見紅色袖子和一點白色抽繩；"
            "不要改女孩臉、表情、髮型、手、紅蘋果、藍色杯子、桌面或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "18_qwen_change_hair_silver_short",
        "artifact_slug": "change_short_silver_hair",
        "title": "Qwen edit: hair to short silver hair",
        "mask_key": None,
        "expected": "女孩頭髮由深色長髮變成銀白短髮或較短淺色髮；臉、表情、手、衣服、蘋果、杯子、桌面和背景應保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用測試原圖。"
            "只把女孩頭髮改成短銀白髮，保留同一張臉、同一表情、同一頭部位置；不要改手、衣服、紅蘋果、藍色杯子、桌面或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "19_qwen_style_watercolor",
        "artifact_slug": "change_watercolor_style",
        "title": "Qwen edit: watercolor style",
        "mask_key": None,
        "expected": "整體變成柔和水彩/紙感風格；人物、蘋果、杯子、桌面和背景構圖應保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用測試原圖。"
            "把整張圖轉成柔和水彩插畫與紙張紋理風格，但保持同一人物、同一表情、同一手部位置、紅蘋果、藍色杯子、桌面、背景和構圖。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
]

PERSON_QWEN_EDIT_CASES: list[dict[str, Any]] = [
    {
        "case_id": "21_person_hair_color_silver",
        "artifact_slug": "change_silver_hair",
        "title": "Person edit: hair color to silver",
        "mask_key": None,
        "expected": "頭髮明確改成銀白色；臉、表情、衣服、手勢與構圖盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用我剛剛提供的人像測試原圖。"
            "只把女孩頭髮改成銀白色，保留同一張臉、同一表情、同一手勢、同一衣服與背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "22_person_hair_style_twin_tails",
        "artifact_slug": "change_twin_tails",
        "title": "Person edit: hairstyle to twin tails",
        "mask_key": None,
        "expected": "髮型改成雙馬尾；髮色、臉、表情、衣服、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "只把女孩髮型改成清楚的 twin tails / 雙馬尾，髮色仍盡量接近原圖深藍黑色；"
            "不要改臉、眼睛、表情、衣服、手勢或背景。解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。"
            "提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "23_person_expression_surprised",
        "artifact_slug": "change_surprised_expression",
        "title": "Person edit: expression to surprised",
        "mask_key": None,
        "expected": "表情變成驚訝，眼睛或嘴型有可見變化；髮型、衣服、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "只把女孩表情改成驚訝表情，眼睛稍微睜大、嘴巴微張；不要改髮型、髮色、衣服、手勢或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "23b_person_change_face_identity",
        "artifact_slug": "change_face_identity",
        "title": "Person edit: change face identity",
        "mask_key": None,
        "expected": "臉部身份明顯改變，例如眼型、臉型、鼻口與臉部氣質不同；髮型、衣服、手勢、身體姿勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "只把女孩的臉換成另一位不同角色的臉：更成熟的臉型、稍微不同的眼型與鼻口比例、神情較冷靜；"
            "不要改髮型、髮色、髮飾、衣服、手勢、身體姿勢或背景，不要加入文字或額外人物。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "23c_person_add_second_person",
        "artifact_slug": "add_second_person",
        "title": "Person edit: add second person",
        "mask_key": None,
        "expected": "畫面新增第二位清楚人物；原本主角臉、髮型、衣服、姿勢與背景盡量保持，不能把原角色替換掉。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "在原本女孩旁邊新增第二位清楚可見的 anime girl friend，站在畫面右側稍後方；"
            "保留原本女孩的臉、髮型、衣服、手勢、身體姿勢與背景，不要把原本女孩替換掉。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "24_person_clothing_red_hoodie",
        "artifact_slug": "change_red_hoodie",
        "title": "Person edit: clothing to red hoodie",
        "mask_key": None,
        "expected": "可見衣服改成紅色連帽衫；臉、髮型、表情、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "只把女孩可見衣服改成紅色連帽衫，可見紅色袖子與白色抽繩；不要改臉、表情、髮型、手勢或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "24b_person_clothing_sailor_uniform",
        "artifact_slug": "change_sailor_uniform",
        "title": "Person edit: clothing to sailor uniform",
        "mask_key": None,
        "expected": "可見衣服改成水手服，包含 sailor collar 與領結；臉、髮型、表情、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "只把女孩可見衣服改成日系水手服，清楚可見 navy sailor collar、白色上衣與紅色領結；"
            "不要改臉、表情、髮型、髮飾、手勢或背景。解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。"
            "提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "24c_person_clothing_swimsuit",
        "artifact_slug": "change_swimsuit",
        "title": "Person edit: clothing to swimsuit",
        "mask_key": None,
        "expected": "可見衣服改成保守泳裝/泳衣風格；臉、髮型、表情、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "只把女孩可見衣服改成保守的一件式泳裝風格，顏色以深藍或黑色為主；"
            "不要改臉、表情、髮型、髮飾、手勢或背景，構圖仍維持半身人像。解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。"
            "提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "24d_person_clothing_kimono",
        "artifact_slug": "change_kimono",
        "title": "Person edit: clothing to kimono",
        "mask_key": None,
        "expected": "可見衣服改成和服，包含衣襟/腰帶等元素；臉、髮型、表情、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "只把女孩可見衣服改成淡色日式和服，清楚可見 kimono collar、袖子與腰帶元素；"
            "不要改臉、表情、髮型、髮飾、手勢或背景。解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。"
            "提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "24e_person_clothing_bikini",
        "artifact_slug": "change_bikini",
        "title": "Person edit: clothing to bikini",
        "mask_key": None,
        "expected": "可見衣服改成兩件式 bikini 泳裝；臉、髮型、表情、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "只把女孩可見衣服改成明確的兩件式 bikini 泳裝，上半身可見 bikini top 與肩帶，顏色以白色或淺藍色為主；"
            "不要改臉、表情、髮型、髮飾、手勢或背景，構圖仍維持半身人像，不要加入文字、水印或額外人物。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "24f_person_clothing_little_devil",
        "artifact_slug": "change_little_devil_costume",
        "title": "Person edit: clothing to little devil costume",
        "mask_key": None,
        "expected": "可見衣服改成小惡魔 cosplay 服裝；應有深色服裝、紅色點綴、小翅膀或惡魔角等元素；臉、髮型、表情、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "只把女孩可見衣服改成可愛小惡魔 cosplay 服裝：黑色或深紫色洋裝、紅色緞帶點綴、可見小惡魔角髮飾，"
            "如果構圖允許可加小蝙蝠翅膀裝飾；不要改臉、表情、主要髮型、手勢或背景，不要加入文字、水印或額外人物。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "25_person_add_hair_accessory",
        "artifact_slug": "add_flower_hair_accessory",
        "title": "Person edit: add flower hair accessory",
        "mask_key": None,
        "expected": "新增粉色花朵髮飾；其他人物特徵、衣服、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "在女孩右側頭髮上新增一個清楚的小粉色花朵髮飾；不要移除原本髮夾，不要改臉、表情、衣服、手勢或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "25b_person_add_cat_ear_hair_accessory",
        "artifact_slug": "add_cat_ears",
        "title": "Person edit: add cat-ear hair accessory",
        "mask_key": None,
        "expected": "新增貓耳髮飾/頭飾；臉、髮型主體、衣服、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "在女孩頭上新增一對清楚的黑色貓耳髮飾或貓耳頭飾，像 cosplay accessory，不要變成真正動物耳朵；"
            "不要改臉、表情、主要髮型、衣服、手勢或背景。解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。"
            "提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "25c_person_remove_hairclips_add_scarf_yandere",
        "artifact_slug": "delete_hairclips_twin_tails_cat_ears_lips_hand_behind_head_tilt_scarf_yandere_lace_large_bust",
        "title": "Person edit: remove hair clips, twin tails, cat ears, lips gesture, hand behind back, head tilt, scarf, yandere, lace outfit, larger bust",
        "mask_key": None,
        "expected": "白色髮夾被移除；髮型改成雙馬尾；新增貓耳髮飾；右手食指觸摸嘴唇；左手伸到背後；頭歪著；頸部新增一條清楚圍巾；表情變成病嬌感；胸部比例變大但身體仍合理；可見服裝改成蕾絲風格；主要人物身份與背景盡量保持。",
        "scoring_items": [
            "白色髮夾被移除且補成自然頭髮",
            "髮型改成雙馬尾",
            "新增清楚貓耳髮飾",
            "右手食指觸摸嘴唇",
            "左手伸到背後",
            "頭歪著",
            "頸部新增清楚紅色或深紅色圍巾",
            "表情變成病嬌感且不血腥",
            "胸部比例變大但身體結構合理",
            "可見服裝改成白色蕾絲洋裝",
            "同一女孩身份與同一背景大致保持",
        ],
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "這次做高難度混合語意編輯：第一，移除女孩頭髮上的白色髮夾，補成自然深藍色頭髮；第二，把女孩髮型改成清楚的 twin tails / 雙馬尾，髮色仍接近原圖深藍黑色；"
            "第三，在頭頂新增清楚貓耳髮飾，和被移除的白色髮夾不同；第四，改成指定動作：右手食指輕輕觸摸嘴唇，左手伸到背後，頭歪著，手指結構要自然；"
            "第五，在脖子周圍新增一條柔軟的紅色或深紅色圍巾，圍巾要清楚可見但不要遮住整張臉；第六，把女孩表情改成病嬌風格，眼神更強烈、微笑略帶危險感，但不要恐怖血腥；"
            "第七，讓胸部比例變大一些，但保持自然身體結構、衣服張力和同一人物身份；第八，把可見白色洋裝改成精緻白色蕾絲洋裝風格，"
            "加入 lace trim、lace fabric details、細緻花邊與褶皺，但保留原本米色外套的大致位置與構圖。"
            "請保留同一個女孩與同一背景，不要加入文字、水印或額外人物。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "25d_person_festival_street_full_body_kimono",
        "artifact_slug": "festival_street_full_body_kimono_single_ponytail_geta_1080x1920",
        "title": "Person edit: vertical full-body festival street, kimono, single ponytail, geta",
        "mask_key": None,
        "expected": "背景改成大街車水馬龍並有模糊路人；尺寸 1080x1920；人物全身入鏡包含腳部；腳穿木屐；衣服改為和服；頭髮改為單馬尾；有日式祭典髮飾；人物身份盡量保持。",
        "scoring_items": [
            "背景改成大街且有車水馬龍/交通感",
            "路人存在且被模糊化或弱化",
            "輸出尺寸為 1080x1920 直式",
            "人物全身入鏡",
            "腳部完整出現且未被裁切",
            "腳上穿著木屐",
            "衣服改為日式祭典和服",
            "頭髮改為單馬尾",
            "有日式祭典髮飾",
            "同一女孩臉部身份與 anime style 大致保持",
        ],
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "這是下一個高難度多功能併發測試：把背景改成大街，車水馬龍，路人可以模糊化但要看得出街道人潮與交通；"
            "尺寸改成 1080x1920 直式構圖，人物需要全身入鏡，從頭到腳都完整出現，不能裁切腳部。"
            "腳上穿著木屐；衣服改為日式祭典和服，清楚可見 kimono collar、袖子、腰帶 obi 與祭典布料細節；"
            "頭髮改為單馬尾，搭配日式祭典應有的髮飾。"
            "請保留同一個女孩的臉部身份與整體 anime style，不要加入文字、水印或額外主角。"
            "解析度 1080x1920，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "25e_person_body_lace_proportions",
        "artifact_slug": "body_taller_slim_waist_larger_bust_long_legs_white_lace_dress_1080x1920",
        "title": "Person edit: body proportions, white lace dress, taller silhouette",
        "mask_key": None,
        "expected": "在全身圖上測體態：人物變更高挑、腰更細、胸部適度變大、腿更修長；衣服改為合身白色蕾絲洋裝；保留同一女孩臉部、單馬尾、祭典髮飾與夜間街景；尺寸 1080x1920；不可裁腳或出現手腳硬傷。",
        "scoring_items": [
            "輸出尺寸為 1080x1920 直式",
            "人物全身入鏡且腳部未裁切",
            "身形更高挑",
            "腰部更細且自然",
            "胸部適度變大且衣服張力合理",
            "腿部更修長且比例合理",
            "服裝改成合身白色蕾絲洋裝",
            "洋裝為不透明且有內襯，不是透明 bodysuit/旗袍/泳裝/內衣",
            "腳部穿著原本木屐或白色鞋履，不是裸足",
            "保留同一女孩臉部身份與 anime style",
            "保留單馬尾與祭典髮飾",
            "保留夜間大街/車流/模糊路人背景",
        ],
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用剛剛通過的全身祭典街景人像圖。"
            "這次專門測試體態與服裝可辨識度：把同一個女孩改成全身站姿的成人感 anime woman，身形更高挑，腰更細，"
            "胸部適度變大但自然，腿部更修長且比例合理；衣服改成合身白色蕾絲 maxi dress，"
            "要有不透明白色布料內襯，lace 只能作為外層裝飾紋理，不能透出軀幹、臀部、大腿或腿部皮膚；"
            "要有 lace fabric texture、lace trim、細緻花邊、真實裙擺與輕微褶皺，但不要裸露、色情化、bodysuit 化、旗袍化、泳裝化或內衣化。"
            "腳部要保留木屐或改成白色鞋履，不可以裸足。"
            "請保留同一張臉、深藍髮色、單馬尾、祭典髮飾、整體 anime style，以及原本夜間大街車流與模糊路人背景。"
            "人物必須完整從頭到腳入鏡，腳部不能裁切；避免六指、缺指、斷手、手腳穿透、身體比例崩壞、文字、水印或額外人物。"
            "解析度 1080x1920，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "26_person_remove_wristband",
        "artifact_slug": "delete_wristband",
        "title": "Person edit: remove wristband",
        "mask_key": None,
        "expected": "手腕黑色飾品被移除；手部、衣服、臉與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "移除女孩手腕上的黑色手環或髮圈，補成自然皮膚或袖口；不要改臉、表情、髮型、衣服、手指姿勢或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "27_person_replace_neck_ribbon",
        "artifact_slug": "replace_ribbon_with_necklace",
        "title": "Person edit: replace ribbon with necklace",
        "mask_key": None,
        "expected": "頸部紅色緞帶改成小金色項鍊；臉、衣服輪廓、手勢與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "把女孩胸口可見的紅色緞帶替換成簡單小金色項鍊；不要改臉、表情、髮型、手勢、外套或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "28_person_repair_fingers",
        "artifact_slug": "repair_fingers",
        "title": "Person edit: repair fingers",
        "mask_key": None,
        "expected": "手指更自然且維持五指；臉、衣服、髮型與背景盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "檢查並修正女孩雙手手指，讓手指更自然、沒有融合、沒有多指、維持五指；不要改臉、表情、髮型、衣服或背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "28b_person_pose_waving",
        "artifact_slug": "change_waving_pose",
        "title": "Person edit: pose to waving hand",
        "mask_key": None,
        "expected": "姿勢改成單手揮手；人物身份、臉、髮型與衣服盡量保持，手部合理。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "把女孩姿勢改成右手抬起揮手，手掌朝向觀眾，五指自然可見；保持同一人物、同一臉、同一髮型、同一衣服與背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "28c_person_pose_v_sign",
        "artifact_slug": "change_v_sign_pose",
        "title": "Person edit: pose to V sign",
        "mask_key": None,
        "expected": "姿勢改成靠臉比 V；人物身份、臉、髮型與衣服盡量保持，手指合理。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "把女孩姿勢改成一隻手在臉旁比 V sign / peace sign，兩根手指清楚自然；不要遮住眼睛或嘴巴，保持同一人物、髮型、衣服與背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "28d_person_pose_arms_crossed",
        "artifact_slug": "change_crossed_arms_pose",
        "title": "Person edit: pose to crossed arms",
        "mask_key": None,
        "expected": "姿勢改成雙手抱胸/交叉手臂；人物身份、臉、髮型與衣服盡量保持，手臂結構合理。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "把女孩姿勢改成雙手抱胸、手臂自然交叉在胸前；保持同一人物、同一臉、同一髮型、同一衣服與背景，不要多手或斷手。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "28f_person_pose_open_arms_complete_occluded_body",
        "artifact_slug": "change_open_arms_complete_occluded_body",
        "title": "Person edit: open arms and complete occluded torso",
        "mask_key": None,
        "expected": "雙臂張開離開胸前；原本被雙手遮住的胸前衣服、肩帶、外套內側與身體輪廓要以原服裝外推補完，不能改領口、緞帶、肩帶、外套版型，也不能多手、斷手或手指嚴重異常。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "把女孩姿勢改成張開雙臂，兩隻手臂向身體左右兩側打開並離開胸前，讓原本被雙手遮住的胸前區域露出來；"
            "請只用原本服裝外推補完被遮住的白色洋裝、同一領口高度、同一紅色緞帶形狀與位置、同一肩帶位置、米色外套邊緣、外套內側、衣服褶皺與身體輪廓。"
            "不要重新設計衣服，不要改領口、緞帶、肩帶、外套版型、顏色或背景。"
            "保持同一人物、同一臉、同一髮型、同一髮夾、同一服裝風格，不要多手、斷手、缺手指、裁切手掌或增加額外人物。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "28g_person_pose_open_arms_bed_outpaint_1920x1080",
        "artifact_slug": "change_open_arms_bed_outpaint_1920x1080",
        "title": "Mixed edit: open arms, bed scene, wide outpaint",
        "mask_key": None,
        "expected": "輸出 1920x1080 橫幅；構圖左右外延且背景改成躺在床上；雙臂張開、雙手完整入鏡；原本遮住的胸前服裝要以原設計補完，不能改領口、緞帶、肩帶、外套版型或顏色。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，這次做混合測試：Qwen Image Edit 2509 語意改圖 + 1920x1080 橫幅外延構圖，"
            "不要使用 inpaint mask；請使用 Qwen Image Edit 2509（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "把畫面變成 1920x1080 橫幅，像左右 outpaint 一樣延伸構圖；把背景與姿勢改成同一個女孩躺在床上，背景有枕頭與柔軟床鋪。"
            "兩隻手臂向左右張開，兩個手掌都要完整留在畫面內，不能裁切手掌。"
            "原本被雙手遮住的胸前區域只用原本服裝外推補完：保持同一領口高度、同一紅色緞帶形狀與位置、同一肩帶位置、同一米色外套邊緣/版型/顏色、同一白色洋裝風格與衣服褶皺。"
            "不要重新設計衣服，不要改臉、髮型、髮夾、緞帶、肩帶、外套版型、色調或增加額外人物；不要多手、斷手、缺手指、裁切手掌、文字或水印。"
            "解析度 1920x1080，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "28e_person_reference_pose_salute",
        "artifact_slug": "copy_reference_pose_salute",
        "title": "Person edit: follow reference pose salute",
        "mask_key": None,
        "reference_pose_key": "salute",
        "expected": "能理解 reference pose 描述並改成敬禮/手在額旁姿勢；人物身份與風格盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "我也提供了一張 pose reference image，請把 source 中女孩改成 reference 圖裡的姿勢：右手抬到額頭旁做 casual salute，左手自然放低；"
            "reference_image_ref 只代表姿勢，不代表換人、換臉或換衣服。保持同一人物、同一髮型、同一衣服與背景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "29_person_style_watercolor",
        "artifact_slug": "change_watercolor_style",
        "title": "Person edit: watercolor style copy",
        "mask_key": None,
        "expected": "整體轉成柔和水彩紙感；人物身份、姿勢、服裝與構圖盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "把整張圖轉成柔和水彩插畫與紙張紋理風格，但保持同一人物、同一姿勢、同一服裝、同一髮型和構圖。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "30_person_background_city_rooftop",
        "artifact_slug": "change_background_city_rooftop",
        "title": "Person edit: background to city rooftop",
        "mask_key": None,
        "expected": "背景明確改成城市屋頂或黃昏/夜景城市感；人物身份、臉、髮型、衣服、手勢與身體姿勢盡量保持。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "只把背景改成黃昏城市屋頂，有遠方建築與暖色天空；不要改女孩的臉、髮型、髮飾、衣服、手勢、身體姿勢或前景。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "31_person_style_semi_realistic",
        "artifact_slug": "change_semi_realistic_style",
        "title": "Person edit: anime to semi-realistic style",
        "mask_key": None,
        "expected": "整體轉成更真實/半寫實插畫質感；人物身份、姿勢、衣服與構圖仍可辨識為同一張圖。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 "
            "（official_workflow_id=origin_qwen_image_edit_2509），source 使用人像測試原圖。"
            "把整張圖改成更真實、半寫實的高品質插畫風格，皮膚、頭髮、布料和光影更接近 realistic illustration；"
            "但保留同一個女孩、同一姿勢、同一衣服、同一髮型、同一背景構圖，不要加入文字、水印或額外人物。"
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。提示詞基礎：by ogipote, anime style, 1girl。"
        ),
    },
    {
        "case_id": "31b_person_style_anything2real_photograph",
        "artifact_slug": "change_anything2real_photograph",
        "title": "Person edit: Anything2Real anime to realistic photograph",
        "mask_key": None,
        "expected": "使用 Anything2RealAlpha LoRA 把 anime 人像轉成更接近真實照片；應保留同一人物、姿勢、服裝與構圖，且明顯比原 Qwen Edit 半寫實基準更接近 photoreal。",
        "natural_language": (
            "請真的使用本站 ComfyUI 圖生圖語意改圖，不要使用 inpaint mask；請使用 Qwen Image Edit 2509 Anything2Real "
            "（official_workflow_id=origin_qwen_image_edit_2509_anything2real），source 使用人像測試原圖。"
            "把整張 anime 圖轉成 realistic photograph，不是 3D 娃娃也不是普通 anime 插畫；"
            "保留同一個女孩、同一張臉的辨識度、同一姿勢、同一服裝輪廓、同一髮型、同一背景構圖，不要加入文字、水印或額外人物。"
            "Use a short English edit instruction internally: transform the image to realistic photograph; preserve the same young woman, face identity, short dark blue hair, blue eyes, hair clips, beige cardigan, white dress, clasped hands, pose, composition, and simple indoor background."
            "解析度 1024x1024，batch 1，steps 4，cfg 1，confirm_billing=true。LoRA strength 0.85。"
        ),
    },
]


def _imported_image_record(image: dict[str, Any]) -> dict[str, Any]:
    image_ref = image.get("image_ref") if isinstance(image.get("image_ref"), dict) else {}
    if not image_ref:
        image_ref = {
            "filename": image.get("filename") or "",
            "subfolder": image.get("subfolder") or "",
            "type": image.get("type") or "input",
        }
    return {
        **image,
        **image_ref,
        "image_ref": image_ref,
        "filename": image.get("filename") or image_ref.get("filename") or "",
        "mime_type": image.get("mime_type") or "image/png",
    }


def _tokens_per_second(chat_response: dict[str, Any], elapsed: float | None) -> float | None:
    usage = chat_response.get("usage") if isinstance(chat_response.get("usage"), dict) else {}
    completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or usage.get("eval_count")
    if not completion_tokens or not elapsed:
        return None
    try:
        return round(float(completion_tokens) / float(elapsed), 3)
    except Exception:
        return None


def _inline_short_english_instruction(prompt: str) -> str:
    match = re.search(
        r"(?:use\s+a\s+short\s+english\s+edit\s+instruction\s+internally|"
        r"short\s+english\s+edit\s+instruction|internal\s+edit\s+instruction)"
        r"\s*[:：]\s*(.+?)\s*$",
        str(prompt or ""),
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip(" \t\r\n\"'`")


def _write_prompt_diagnostics(write_args: dict[str, Any]) -> dict[str, Any]:
    prompt = str(write_args.get("prompt") or "")
    edit_instruction = str(write_args.get("edit_instruction") or write_args.get("edit_prompt") or "")
    inline_instruction = _inline_short_english_instruction(prompt)
    prompt_has_cjk = bool(re.search(r"[\u3400-\u9fff]", prompt))
    structured = bool(edit_instruction.strip())
    return {
        "prompt_length": len(prompt),
        "edit_instruction_length": len(edit_instruction.strip()),
        "prompt_has_cjk": prompt_has_cjk,
        "prompt_has_inline_short_english_instruction": bool(inline_instruction),
        "inline_instruction_preview": inline_instruction[:240],
        "llm_structured_edit_instruction": structured,
        "backend_normalization_required": bool((inline_instruction or prompt_has_cjk) and not structured),
        "risk": (
            "backend_normalization_required"
            if (inline_instruction or prompt_has_cjk) and not structured
            else ("mixed_natural_language_prompt" if prompt_has_cjk and not structured else "ok")
        ),
    }


def make_pose_reference_assets(out_dir: Path, reference_image: Path | None = None) -> dict[str, Path]:
    asset_dir = out_dir / "assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    path = asset_dir / "reference_pose_salute_1024x1024.png"
    if reference_image is not None:
        image = Image.open(reference_image).convert("RGB")
        image.thumbnail((IMAGE_WIDTH, IMAGE_HEIGHT), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), (245, 246, 248))
        x = (IMAGE_WIDTH - image.width) // 2
        y = (IMAGE_HEIGHT - image.height) // 2
        canvas.paste(image, (x, y))
        canvas.save(path)
        return {"salute": path}

    image = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), (245, 246, 248))
    draw = ImageDraw.Draw(image)
    skin = (238, 182, 145)
    hair = (42, 50, 76)
    clothes = (72, 102, 166)
    line = (28, 32, 42)
    cx = IMAGE_WIDTH // 2
    head_r = 108
    head_box = (cx - head_r, 130, cx + head_r, 130 + head_r * 2)
    draw.ellipse(head_box, fill=skin, outline=line, width=8)
    draw.pieslice((cx - 125, 104, cx + 125, 310), 180, 360, fill=hair, outline=line, width=6)
    draw.rectangle((cx - 42, 320, cx + 42, 380), fill=skin, outline=line, width=5)
    draw.rounded_rectangle((cx - 150, 380, cx + 150, 720), radius=54, fill=clothes, outline=line, width=8)
    draw.line((cx - 135, 430, cx - 330, 610), fill=line, width=34)
    draw.line((cx - 330, 610, cx - 360, 720), fill=line, width=34)
    draw.line((cx + 135, 430, cx + 265, 260), fill=line, width=34)
    draw.line((cx + 265, 260, cx + 120, 230), fill=line, width=34)
    draw.ellipse((cx + 90, 202, cx + 150, 262), fill=skin, outline=line, width=6)
    draw.line((cx - 85, 760, cx - 170, 950), fill=line, width=38)
    draw.line((cx + 85, 760, cx + 170, 950), fill=line, width=38)
    draw.line((cx - 42, 214, cx - 10, 214), fill=line, width=7)
    draw.line((cx + 10, 214, cx + 42, 214), fill=line, width=7)
    draw.arc((cx - 45, 242, cx + 45, 294), 15, 165, fill=line, width=6)
    image.save(path)
    return {"salute": path}


def seed_context_with_reference(page, source: dict[str, Any], mask: dict[str, Any] | None, reference: dict[str, Any] | None, case: dict[str, Any]) -> None:
    seed_context(page, source, mask, case)
    if not reference:
        return
    page.evaluate(
        """({reference, caseInfo}) => {
          AI_AGENT_STATE.messages.push({
            role: "assistant",
            content: `pose reference image for ${caseInfo.case_id}: 這張是動作參考圖，請把這張當 reference_image_ref；它只代表姿勢/動作，不代表換臉、換人、換衣服或換背景。`,
            images: [{image_ref: reference.image_ref, cloud_file_id: reference.cloud_file_id || "", storage_file_id: reference.storage_file_id || "", filename: reference.filename, mime_type: reference.mime_type || "image/png"}],
          });
          renderAiAgentThread();
        }""",
        {"reference": reference, "caseInfo": {"case_id": case["case_id"]}},
    )


def _artifact_slug(case: dict[str, Any]) -> str:
    raw = str(case.get("artifact_slug") or case.get("case_id") or "case").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return slug or "case"


def _prompt_slug(text: str, *, limit: int = 64) -> str:
    words = re.findall(r"[a-zA-Z0-9]+", str(text or "").lower())
    slug = "_".join(words[:12]).strip("_")
    if not slug:
        slug = "prompt"
    return slug[:limit].strip("_") or "prompt"


def _copy_artifact(src: Path, dst: Path) -> str:
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return str(dst)
    return ""


def _source_asset_name_for_size(width: int, height: int) -> str:
    if width == IMAGE_WIDTH and height == IMAGE_HEIGHT:
        return SOURCE_IMAGE_NAME
    return f"source_{width}x{height}.png"


def _preserve_source_asset_if_needed(out_dir: Path, source_path: Path, asset_paths: dict[str, Path]) -> tuple[dict[str, Path], str, str]:
    with Image.open(source_path) as source_image:
        source_width, source_height = source_image.size
        source_asset_name = _source_asset_name_for_size(source_width, source_height)
        source_resolution = f"{source_width}x{source_height}"
        if source_width == IMAGE_WIDTH and source_height == IMAGE_HEIGHT:
            asset_paths["source_name"] = Path(SOURCE_IMAGE_NAME)
            return asset_paths, SOURCE_IMAGE_NAME, source_resolution
        preserved_source = out_dir / "assets" / source_asset_name
        preserved_source.parent.mkdir(parents=True, exist_ok=True)
        if source_path.resolve() != preserved_source.resolve():
            source_image.convert("RGB").save(preserved_source)
    asset_paths["source"] = preserved_source
    asset_paths["source_name"] = Path(source_asset_name)
    metadata_path = out_dir / "assets" / "source_metadata.json"
    metadata_path.write_text(
        json.dumps(
            {
                "base_prompt": BASE_PROMPT,
                "source_scene_prompt": SOURCE_SCENE_PROMPT,
                "source_generation_case": "existing_source_import",
                "visible_prompt_text_in_image": False,
                "note": (
                    "Existing-source i2i audit preserved the source aspect ratio; "
                    "do not silently resize portrait/landscape sources to 1024x1024."
                ),
                "source_resolution": source_resolution,
                "normalized_to_fixed_square": False,
                "test_targets": ["existing_source_i2i"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return asset_paths, source_asset_name, source_resolution


def _artifact_report_path(value: str, out_dir: Path) -> str:
    if not value:
        return value
    path = Path(value)
    if not path.is_absolute():
        return value
    try:
        return str(path.relative_to(out_dir))
    except ValueError:
        return str(path)


def write_case_artifact_pack(
    *,
    out_dir: Path,
    case: dict[str, Any],
    source_path: Path,
    reference_path: Path | None,
    result_path: Path | None,
    case_report: dict[str, Any],
) -> dict[str, str]:
    slug = _artifact_slug(case)
    write_arguments = case_report.get("write_arguments") if isinstance(case_report.get("write_arguments"), dict) else {}
    prompt_slug = _prompt_slug(
        write_arguments.get("edit_instruction")
        or write_arguments.get("prompt")
        or case_report.get("natural_language")
        or case.get("natural_language")
        or ""
    )
    pass_fail = (
        "pass"
        if case_report.get("job_status") == "completed"
        and (case_report.get("result_preview") or {}).get("ok")
        and not (case_report.get("visual_artifacts") or {}).get("has_blocking_artifact")
        else "fail"
    )
    run_label = "run01"
    result_name = f"{slug}_{prompt_slug}_{pass_fail}_{run_label}.png"
    case_dir = out_dir / "i2i_cases" / slug
    shared_case_dir = Path("/mnt/c/share/Comfyui/output/i2i") / slug
    artifacts = {
        "case_dir": str(case_dir),
        "shared_case_dir": str(shared_case_dir),
        "origin": _copy_artifact(source_path, case_dir / "origin.png"),
        "ref": "",
        "result": "",
        "shared_origin": "",
        "shared_ref": "",
        "shared_result": "",
        "summary": str(case_dir / "CASE_SUMMARY.md"),
        "shared_summary": str(shared_case_dir / "CASE_SUMMARY.md"),
    }
    artifacts["shared_origin"] = _copy_artifact(source_path, shared_case_dir / "origin.png")
    if reference_path is not None:
        artifacts["ref"] = _copy_artifact(reference_path, case_dir / "ref.png")
        artifacts["shared_ref"] = _copy_artifact(reference_path, shared_case_dir / "ref.png")
    if result_path is not None:
        artifacts["result"] = _copy_artifact(result_path, case_dir / result_name)
        artifacts["shared_result"] = _copy_artifact(result_path, shared_case_dir / result_name)
    summary = case_dir / "CASE_SUMMARY.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(
        "\n".join([
            f"# {slug}",
            "",
            f"- case_id: `{case.get('case_id', '')}`",
            f"- title: {case.get('title', '')}",
            f"- expected: {case.get('expected', '')}",
            f"- natural_language: {case_report.get('natural_language', '')}",
            f"- job_status: {case_report.get('job_status', '')}",
            f"- elapsed_seconds: {case_report.get('elapsed_seconds', '')}",
            f"- planner_elapsed_seconds: {case_report.get('planner_elapsed_seconds', '')}",
            f"- tokens_per_second: {case_report.get('tokens_per_second', '')}",
            f"- model: {case_report.get('chat_model', '')}",
            f"- scoring_policy: {case_report.get('scoring_policy', {}).get('pass_threshold', '') if isinstance(case_report.get('scoring_policy'), dict) else ''}",
            f"- hard_fail_rule: {case_report.get('scoring_policy', {}).get('hard_fail_rule', '') if isinstance(case_report.get('scoring_policy'), dict) else ''}",
            f"- prompt_achievement_rate: {case_report.get('prompt_achievement_rate', '')}",
            f"- hard_fail_detected: {case_report.get('hard_fail_detected', '')}",
            f"- scoring_items: {json.dumps(case_report.get('scoring_items') or [], ensure_ascii=False)}",
            f"- result_file: `{Path(artifacts['result']).name if artifacts.get('result') else ''}`",
            f"- origin_file: `origin.png`",
            f"- reference_file: `ref.png`" if artifacts.get("ref") else "- reference_file: ``",
            f"- filename_rule: `origin.png`, `ref.png`, `<feature>_<prompt_summary>_<pass|fail>_<runNN>.png`",
            "",
        ]),
        encoding="utf-8",
    )
    shared_summary = shared_case_dir / "CASE_SUMMARY.md"
    try:
        shared_summary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(summary, shared_summary)
    except OSError:
        pass
    return artifacts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:5000")
    parser.add_argument("--username", default="root")
    parser.add_argument("--root-password", default="root")
    parser.add_argument("--source-image", default="")
    parser.add_argument(
        "--source-fixture",
        choices=["", "six-finger-hand"],
        default="",
        help="Generate a controlled source fixture instead of importing --source-image.",
    )
    parser.add_argument("--source-label", default="human_accepted_v5")
    parser.add_argument(
        "--source-origin-type",
        default="",
        help="Override report source_origin_type when the source is proven by a separate live source-gate report.",
    )
    parser.add_argument("--source-gate-report", default="")
    parser.add_argument(
        "--reference-image",
        default="",
        help="Optional real pose reference image for reference-pose cases; normalized to 1024x1024 assets.",
    )
    parser.add_argument("--mask-preset", default="accepted_v5")
    parser.add_argument("--case-set", choices=["flux-fill", "qwen-edit", "person-qwen", "all"], default="flux-fill")
    parser.add_argument("--case-id", default="", help="Run only one case_id from the selected case set.")
    parser.add_argument("--out-dir", default="")
    parser.add_argument("--job-timeout-seconds", type=int, default=7200)
    parser.add_argument("--model", default="qwen3.5:cloud")
    parser.add_argument("--api-base-url", default="http://127.0.0.1:11434/v1")
    parser.add_argument("--comfyui-api-url", default="http://127.0.0.1:8189")
    parser.add_argument(
        "--denoise-strength",
        type=float,
        default=None,
        help="Optional natural-language denoise_strength hint appended to each generated command.",
    )
    parser.add_argument(
        "--instruction-suffix",
        default="",
        help="Optional natural-language suffix appended to each generated command.",
    )
    args = parser.parse_args()

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir or f"/tmp/hackme_ai_agent_existing_source_i2i_{stamp}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    result_dir = out_dir / "results"
    result_dir.mkdir(parents=True, exist_ok=True)

    if args.source_fixture == "six-finger-hand":
        fixture_paths = make_six_finger_repair_fixture(out_dir)
        source_path = fixture_paths["source"].resolve()
        if args.mask_preset == "accepted_v5":
            args.mask_preset = "six_finger_fixture"
    else:
        if not args.source_image:
            raise ValueError("--source-image is required unless --source-fixture is set")
        source_path = Path(args.source_image).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

    reference_image_path = Path(args.reference_image).resolve() if args.reference_image else None
    if reference_image_path is not None and not reference_image_path.is_file():
        raise FileNotFoundError(reference_image_path)

    source_origin_type = args.source_origin_type or ("diagnostic_fixture" if args.source_fixture else "preexisting_source")
    with Image.open(source_path) as initial_source_image:
        initial_source_width, initial_source_height = initial_source_image.size
    source_resolution_label = f"{initial_source_width}x{initial_source_height}"
    source_asset_name = _source_asset_name_for_size(initial_source_width, initial_source_height)

    report: dict[str, Any] = {
        "ok": False,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": args.base_url.rstrip("/"),
        "login_username": args.username,
        "base_prompt": BASE_PROMPT,
        "source_scene_prompt": SOURCE_SCENE_PROMPT,
        "source_origin_type": source_origin_type,
        "source_gate_report": args.source_gate_report,
        "delivery_acceptance_rule": (
            "This script is diagnostic only unless the imported source is separately proven to be "
            "a live site-generated txt2img source from the current audit cycle. Do not mark final "
            "i2i capability as passed from synthetic/simple fixtures."
        ),
        "source_image_visible_prompt_text": False,
        "fixed_resolution": source_resolution_label,
        "fixed_quality": "SDXL grade, batch 1",
        "source_image_rel": f"assets/{source_asset_name}",
        "source_label": args.source_label,
        "source_original_path": str(source_path),
        "reference_original_path": str(reference_image_path) if reference_image_path else "",
        "mask_preset": args.mask_preset,
        "visual_scoring_policy": VISUAL_SCORING_POLICY,
        "cases": [],
        "browser_errors": [],
    }
    if args.source_fixture == "six-finger-hand":
        report["source_scene_prompt"] = (
            f"{BASE_PROMPT}, controlled six-finger hand repair fixture, one clear right hand on a simple tabletop, "
            "six visible fingers before repair, remove the rightmost extra finger, no text, no watermark"
        )
        report["delivery_acceptance_rule"] = (
            "Controlled six-finger source is a diagnostic fixture only. It can classify workflow/model "
            "failure causes, but it cannot be used as final delivery evidence."
        )
    elif source_origin_type == "real_t2i_source":
        report["delivery_acceptance_rule"] = (
            "Source image is marked as a live site-generated txt2img source from this audit cycle. "
            "Use source_gate_report and visual verdict to decide whether each downstream i2i case "
            "is delivery evidence for that capability."
        )

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

        asset_paths = make_mask_assets_for_source(out_dir, source_path, mask_preset=args.mask_preset)
        asset_paths, source_asset_name, source_resolution_label = _preserve_source_asset_if_needed(out_dir, source_path, asset_paths)
        report["fixed_resolution"] = source_resolution_label
        report["source_image_rel"] = f"assets/{source_asset_name}"
        reference_paths = make_pose_reference_assets(out_dir, reference_image_path)
        source_imported = _imported_image_record(import_image(page, asset_paths["source"], source_asset_name))
        imported = {
            "source": source_imported,
            "apple": import_image(page, asset_paths["mask_apple"], f"agent_i2i_mask_apple_{IMAGE_WIDTH}x{IMAGE_HEIGHT}.png"),
            "cup": import_image(page, asset_paths["mask_cup"], f"agent_i2i_mask_cup_{IMAGE_WIDTH}x{IMAGE_HEIGHT}.png"),
            "hand": import_image(page, asset_paths["mask_hand"], f"agent_i2i_mask_hand_{IMAGE_WIDTH}x{IMAGE_HEIGHT}.png"),
            "references": {
                key: _imported_image_record(import_image(page, path, path.name))
                for key, path in reference_paths.items()
            },
        }
        report["imported_images"] = imported
        report["reference_images"] = {
            key: f"assets/{path.name}"
            for key, path in reference_paths.items()
        }
        source_visual_artifacts = detect_visual_artifacts(asset_paths["source"])
        report["source_generation"] = {
            "case_id": "00_existing_source",
            "title": "Existing human-accepted source image",
            "source_origin_type": report["source_origin_type"],
            "delivery_gate": "diagnostic_only",
            "natural_language": "沿用前一輪人工判定可接受的 v5 生圖結果作為 i2i source，不重新抽圖。",
            "expected": f"固定使用同一張 {source_resolution_label} SDXL 等級原圖，後續 i2i 測試不得因重新產圖造成基準漂移。",
            "status": "imported",
            "elapsed_seconds": 0,
            "planner_elapsed_seconds": None,
            "chat_model": "-",
            "usage": {},
            "tokens_per_second": None,
            "write_tool": "-",
            "write_arguments": {"source_image": str(source_path), "mask_preset": args.mask_preset},
            "job_id": "",
            "job_status": "imported",
            "result_preview": {"ok": True, "path": str(asset_paths["source"])},
            "result_image_rel": f"assets/{source_asset_name}",
            "mask_overlay_rel": "",
            "thread_anomaly_metrics": {},
            "visual_artifacts": source_visual_artifacts,
            "manual_score": "85%",
            "manual_judgement": "人工目視接受：臉部、畫風與主體品質可用；物件左右與構圖不完全符合原嚴格規格，作為 i2i 壓測基準固定使用。",
        }

        selected_cases = []
        if args.case_set in {"flux-fill", "all"}:
            selected_cases.extend(CASES)
        if args.case_set in {"qwen-edit", "all"}:
            selected_cases.extend(QWEN_EDIT_CASES)
        if args.case_set in {"person-qwen", "all"}:
            selected_cases.extend(PERSON_QWEN_EDIT_CASES)
        if args.case_id:
            selected_cases = [case for case in selected_cases if case.get("case_id") == args.case_id]
            if not selected_cases:
                raise ValueError(f"case_id not found in {args.case_set}: {args.case_id}")
        report["case_set"] = args.case_set
        report["case_id_filter"] = args.case_id
        report["denoise_strength_hint"] = args.denoise_strength
        report["instruction_suffix"] = args.instruction_suffix

        for case in selected_cases:
            artifact_slug = _artifact_slug(case)
            before_chat = len(chat_events)
            before_writes = len(write_events)
            mask_key = case.get("mask_key")
            mask = imported.get(mask_key) if mask_key else None
            reference_key = str(case.get("reference_pose_key") or "").strip()
            reference = (imported.get("references") or {}).get(reference_key) if reference_key else None
            seed_context_with_reference(page, imported["source"], mask, reference, case)
            preflight = ai_agent_preflight(page)
            start = time.perf_counter()
            natural_language = str(case["natural_language"])
            if args.denoise_strength is not None:
                if mask:
                    natural_language += (
                        f" 請設定 denoise_strength={args.denoise_strength:g}；"
                        "只允許遮罩內依本次目標重繪，遮罩外盡量保持原構圖、人物與背景。"
                    )
                else:
                    natural_language += (
                        f" 請設定 denoise_strength={args.denoise_strength:g}；"
                        "這是無遮罩 img2img 語意編輯，請大幅套用本次明確指定的目標變更，"
                        "但非目標元素如同一張臉、髮色、單馬尾、髮飾與夜間街景盡量保持。"
                    )
            if args.instruction_suffix:
                natural_language += f" {args.instruction_suffix}"
            send_result = send_ai_agent_message(page, natural_language)

            deadline = time.time() + 15
            while time.time() < deadline and len(write_events) == before_writes:
                text = thread_text(page)
                if "需要補充" in text or "未送出" in text:
                    break
                time.sleep(1)

            case_writes = write_events[before_writes:]
            case_chats = chat_events[before_chat:]
            write = case_writes[-1] if case_writes else {}
            write_response = write.get("response") if isinstance(write.get("response"), dict) else {}
            write_request = write.get("request") if isinstance(write.get("request"), dict) else {}
            write_args = write_request.get("arguments") if isinstance(write_request.get("arguments"), dict) else {}
            tool = write_request.get("tool") or ""
            chat_response = case_chats[-1]["response"] if case_chats and isinstance(case_chats[-1].get("response"), dict) else {}
            planner_plan = extract_json_object(
                (chat_response.get("message") or {}).get("content")
                if isinstance(chat_response.get("message"), dict)
                else ""
            )
            if not write_args and isinstance(planner_plan.get("args"), dict):
                write_args = planner_plan["args"]
            if not tool and planner_plan.get("tool"):
                tool = str(planner_plan.get("tool") or "")

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
                    preview = save_preview_with_retry(page, image["image_ref"], result_dir / f"{artifact_slug}_result.png")

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

            usage = chat_response.get("usage") if isinstance(chat_response.get("usage"), dict) else {}
            planner_elapsed = case_chats[-1].get("elapsed_seconds") if case_chats else None
            messages = thread_messages(page)
            scoring_items = _case_scoring_items(case)
            hard_fail_detected = bool(visual_artifacts.get("has_blocking_artifact"))
            case_report = {
                "case_id": case["case_id"],
                "artifact_slug": artifact_slug,
                "title": case["title"],
                "natural_language": natural_language,
                "expected": case["expected"],
                "base_prompt": BASE_PROMPT,
                "status": "submitted" if job_id else ("tool_failed" if write_failed else "not_submitted"),
                "elapsed_seconds": round(time.perf_counter() - start, 3),
                "planner_elapsed_seconds": planner_elapsed,
                "chat_model": chat_response.get("model") or "",
                "usage": usage,
                "tokens_per_second": _tokens_per_second(chat_response, planner_elapsed),
                "write_tool": tool,
                "write_arguments": write_args,
                "write_prompt_diagnostics": _write_prompt_diagnostics(write_args),
                "write_response": write_response,
                "job_id": job_id,
                "job_status": effective_job_status or ("tool_failed" if write_failed else ""),
                "raw_job_status": raw_job_status,
                "job": job,
                "job_polls": polls,
                "result_preview": preview,
                "result_image_rel": str(Path(preview.get("path", "")).relative_to(out_dir)) if preview.get("ok") else "",
                "mask_overlay_rel": f"assets/mask_{mask_key}_overlay.png" if mask_key else "",
                "reference_image_rel": f"assets/{reference_paths[reference_key].name}" if reference_key and reference_key in reference_paths else "",
                "chat_events": case_chats,
                "write_events": case_writes,
                "thread_tail": thread_text(page)[-6000:],
                "thread_anomaly_metrics": anomaly_metrics(messages),
                "visual_artifacts": visual_artifacts,
                "scoring_policy": VISUAL_SCORING_POLICY,
                "scoring_items": scoring_items,
                "hard_fail_detected": hard_fail_detected,
                "prompt_achievement_rate": "0%" if hard_fail_detected else "待人工逐項判定",
                "prompt_achievement_scoring_note": (
                    "硬性瑕疵優先：若有六指/多指、肢體或衣物不合理穿越、必須出現部位裁切、嚴重解剖錯誤、黑灰空圖等，直接不合格。"
                    "無硬性瑕疵時才按 scoring_items 達成率計分。"
                ),
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
            reference_asset_path = reference_paths.get(reference_key) if reference_key and reference_key in reference_paths else None
            result_asset_path = Path(preview["path"]) if preview.get("ok") else None
            artifact_pack = write_case_artifact_pack(
                out_dir=out_dir,
                case=case,
                source_path=asset_paths["source"],
                reference_path=reference_asset_path,
                result_path=result_asset_path,
                case_report=case_report,
            )
            case_report["artifact_pack"] = {
                key: _artifact_report_path(value, out_dir)
                for key, value in artifact_pack.items()
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

        screenshot = out_dir / "ai_agent_i2i_existing_source_final.png"
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
