import json
import os
import re
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta
import shutil
from time import time
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urljoin, urlparse


DEFAULT_AI_AGENT_API_BASE_URL = os.environ.get("HACKME_AI_AGENT_API_BASE_URL", "http://127.0.0.1:8642/v1")
DEFAULT_AI_AGENT_MODEL = os.environ.get("HACKME_AI_AGENT_MODEL", "hermes-agent")
DEFAULT_AI_AGENT_PROVIDER = "hermes"
DEFAULT_AI_AGENT_PERSONA = "concise_helper"
DEFAULT_AI_AGENT_OPERATION_MODE = "readonly"
MAX_AI_AGENT_IMAGE_DATA_URL_CHARS = 3 * 1024 * 1024
AI_AGENT_AUDIT_INTERVAL_MINUTES_DEFAULT = 5
AI_AGENT_AUDIT_INTERVAL_MINUTES_MAX = 60
AI_AGENT_OPERATION_MODES = {"readonly", "assist", "write", "audit"}
AI_AGENT_ROLE_RANK = {"user": 0, "manager": 1, "super_admin": 2}
AI_AGENT_OPERATION_MODE_POLICIES = {
    "readonly": {
        "label": "唯讀",
        "description": "只回答查詢、導覽、排錯與狀態判讀；拒絕刪除、重啟、封鎖、寫入等操作型要求。",
        "write_enabled": False,
        "audit_enabled": False,
        "min_role": "user",
    },
    "assist": {
        "label": "協助",
        "description": "可提供站內操作建議與草稿，但不直接修改系統狀態。",
        "write_enabled": False,
        "audit_enabled": False,
        "min_role": "user",
    },
    "write": {
        "label": "執行寫入",
        "description": "root 專用白名單工具型任務；仍需通過任務白名單與伺服器端 API 檢查。",
        "write_enabled": True,
        "audit_enabled": False,
        "min_role": "super_admin",
    },
    "audit": {
        "label": "僅審計",
        "description": "週期檢查 logs、審計資料、資源、網路流量與 IP 請求異常；root 可檢視完整掃描與觸發手動掃描。",
        "write_enabled": False,
        "audit_enabled": True,
        "min_role": "manager",
    },
}
AI_AGENT_AUDIT_IP_EVENT_RATE_THRESHOLD_DEFAULT = 240
AI_AGENT_AUDIT_IP_EVENT_RATE_WINDOW_MINUTES_DEFAULT = 5
AI_AGENT_AUDIT_SECURITY_EVENT_RATE_THRESHOLD_DEFAULT = 120
AI_AGENT_AUDIT_SECURITY_EVENT_RATE_WINDOW_MINUTES_DEFAULT = 5
AI_AGENT_AUDIT_CPU_PERCENT_THRESHOLD_DEFAULT = 92
AI_AGENT_AUDIT_RAM_PERCENT_THRESHOLD_DEFAULT = 92
AI_AGENT_AUDIT_DISK_PERCENT_THRESHOLD_DEFAULT = 95
AI_AGENT_AUDIT_AUTO_BLOCK_DEFAULT = False
AI_AGENT_AUDIT_BLOCK_MINUTES_DEFAULT = 30
AI_AGENT_AUDIT_NOTIFY_ROOT_DEFAULT = False
AI_AGENT_AUDIT_IP_EVENT_RATE_MAX_PER_MIN = 10000
AI_AGENT_AUDIT_SECURITY_EVENT_RATE_MAX_PER_MIN = 10000
KNOWN_MOCK_CHAT_REPLIES = {
    "mockhermesresponse已收到你的請求",
    "mockhermesresponse已收到你的请求",
}

_AUDIT_SCAN_STATE = {
    "audit": {
        "at": 0.0,
        "data": {},
    },
    "network_last": {
        "at": 0.0,
        "data": {},
    }
}
_AUDIT_SCAN_LOCK = threading.Lock()
def _compact_mock_text(value):
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = text.replace("\u3000", "")
    text = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", text)
    return text



def _has_mock_request_phrase(text):
    return "已收到你的請求" in text or "已收到你的请求" in text


def _contains_mock_phrase(value):
    if isinstance(value, str):
        compact = _compact_mock_text(value)
        if not compact:
            return False
        if compact in KNOWN_MOCK_CHAT_REPLIES:
            return True
        if "mockhermesresponse" in compact and _has_mock_request_phrase(compact):
            return True
        return False
    if isinstance(value, dict):
        for item in value.values():
            if _contains_mock_phrase(item):
                return True
        return False
    if isinstance(value, list):
        for item in value:
            if _contains_mock_phrase(item):
                return True
        return False
    return False


def parse_int_setting(settings, key, default, minimum, maximum):
    try:
        value = int((settings or {}).get(key, default))
    except Exception:
        value = default
    return max(minimum, min(maximum, value))


def normalize_ai_agent_allowed_models(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [str(item or "").strip() for item in value]
    else:
        raw = str(value)
        if "\n" in raw or "\r" in raw or "\t" in raw:
            return None
        parts = [part.strip() for part in str(value).replace("\n", ",").split(",")]
    models = []
    seen = set()
    for part in parts:
        if not part:
            continue
        if any(ch in part for ch in "\r\n\t"):
            return None
        model = part.strip()
        if len(model) > 200:
            return None
        if model not in seen:
            seen.add(model)
            models.append(model)
    return ",".join(models)


def normalize_ai_agent_allowed_tools(value):
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        parts = [str(item or "").strip() for item in value]
    else:
        raw = str(value)
        if "\n" in raw or "\r" in raw or "\t" in raw:
            return None
        parts = [part.strip() for part in raw.split(",")]
    tools = []
    seen = set()
    valid = set(AI_AGENT_TOOL_BLUEPRINT)
    for part in parts:
        if not part:
            continue
        if part not in valid:
            return None
        if part not in seen:
            seen.add(part)
            tools.append(part)
    return ",".join(tools)


def clear_ai_agent_audit_scan_state():
    with _AUDIT_SCAN_LOCK:
        _AUDIT_SCAN_STATE["audit"] = {"at": 0.0, "data": {}}
        _AUDIT_SCAN_STATE["network_last"] = {"at": 0.0, "data": {}}


def normalize_ai_agent_operation_mode(value):
    raw = str(value or "").strip().lower()
    if raw in AI_AGENT_OPERATION_MODES:
        return raw
    if raw in {"read_only", "read"}:
        return "readonly"
    if raw in {"write_candidate", "execution", "action"}:
        return "write"
    return None


def ai_agent_operation_mode_policy(mode):
    normalized = normalize_ai_agent_operation_mode(mode) or DEFAULT_AI_AGENT_OPERATION_MODE
    policy = dict(AI_AGENT_OPERATION_MODE_POLICIES.get(normalized, AI_AGENT_OPERATION_MODE_POLICIES[DEFAULT_AI_AGENT_OPERATION_MODE]))
    policy["mode"] = normalized
    return policy


def normalize_ai_agent_audit_interval_minutes(value, *, default=None):
    default = int(default if default is not None else AI_AGENT_AUDIT_INTERVAL_MINUTES_DEFAULT)
    return parse_int_setting(
        {"ai_agent_audit_interval_minutes": value},
        "ai_agent_audit_interval_minutes",
        default,
        1,
        AI_AGENT_AUDIT_INTERVAL_MINUTES_MAX,
    )


def normalize_ai_agent_audit_int(value, key, *, default, minimum, maximum):
    return parse_int_setting({key: value}, key, default, minimum, maximum)


def normalize_ai_agent_audit_bool(value, default):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "on", "yes", "y"}:
        return True
    if raw in {"0", "false", "off", "no", "n", ""}:
        return False
    return bool(default)


AI_AGENT_PERSONA_PRESETS = {
    "concise_helper": {
        "label": "簡潔客服導向",
        "guidance": "請保持回應簡潔、步驟明確，先判斷使用者在網站流程中的位置。",
        "tone": "平實、可執行導向。",
    },
    "strict_helper": {
        "label": "嚴謹流程助手",
        "guidance": "回應請先列出確認條件，逐步排查，附上檢查順序與結果判讀。",
        "tone": "保守、結構化。",
    },
    "creative_coordinator": {
        "label": "創意流程統籌",
        "guidance": "回應時提供清楚的提示詞與參數建議，但要先確認任務範圍是否符合站內功能。",
        "tone": "有組織、鼓勵式。",
    },
}

AI_AGENT_TASKS = {
    "site_guide": {
        "label": "網站導覽",
        "description": "回答站內功能位置、按鈕位置、流程步驟。",
        "safe_reply": "若未啟用，請回覆請先開啟「網站導覽」。",
    },
    "troubleshoot": {
        "label": "生圖 / 下載排錯",
        "description": "協助檢查生圖、下載、模型載入、輸出與報錯流程，不做實際操作。",
        "safe_reply": "若未啟用，請回覆請先開啟「生圖 / 下載排錯」。",
    },
    "prompt": {
        "label": "生圖提示詞與參數",
        "description": "提供提示詞、尺寸、步數與常見參數建議，但不直接執行。",
        "safe_reply": "若未啟用，請回覆請先開啟「生圖提示詞與參數」。",
    },
}

AI_AGENT_TOOL_BLUEPRINT = {
    "check_resource_state": {
        "label": "資源快照",
        "description": "查看 CPU、RAM、磁碟與基本服務狀態。",
        "min_role": "user",
        "data_scope": "own_session",
    },
    "check_generation_progress": {
        "label": "產圖進度",
        "description": "查看目前登入用戶自己的 ComfyUI 產圖任務進度。",
        "min_role": "user",
        "data_scope": "own_user",
    },
    "check_download_progress": {
        "label": "下載排查",
        "description": "查看目前登入用戶自己的下載任務與錯誤摘要。",
        "min_role": "user",
        "data_scope": "own_user",
    },
    "inspect_user_files": {
        "label": "檔案快照",
        "description": "查看雲端硬碟檔案摘要；一般用戶只限自己的檔案，root 可看全站摘要。",
        "min_role": "user",
        "data_scope": "own_user_or_root_all",
    },
    "check_download_state": {
        "label": "下載排查",
        "description": "依下載、輸出與錯誤訊息提供下一步檢查順序。",
        "min_role": "user",
        "data_scope": "own_user",
    },
    "suggest_navigation_step": {
        "label": "導覽建議",
        "description": "指出網站畫面、頁面與操作路徑。",
        "min_role": "user",
        "data_scope": "public_site",
    },
    "suggest_prompt": {
        "label": "提示詞建議",
        "description": "提供可直接複製調整的提示詞與參數草稿。",
        "min_role": "user",
        "data_scope": "user_prompt",
    },
    "member_management_readonly": {
        "label": "會員管理唯讀",
        "description": "查看會員統計與帳號狀態摘要；目前僅 manager 以上可用。",
        "min_role": "manager",
        "data_scope": "member_summary",
    },
    "attack_diagnosis_readonly": {
        "label": "攻擊診斷唯讀",
        "description": "查看安全事件、失敗任務與攻擊跡象摘要；root 專用。",
        "min_role": "super_admin",
        "data_scope": "server_security_summary",
    },
    "audit_status": {
        "label": "審計狀態",
        "description": "查看 AI Agent 僅審計 worker 的狀態與摘要；root 專用。",
        "min_role": "super_admin",
        "data_scope": "audit_summary",
    },
    "audit_scan": {
        "label": "立即審計掃描",
        "description": "手動觸發 logs、審計資料、網路流量、IP 請求與資源異常掃描；root 專用。",
        "min_role": "super_admin",
        "data_scope": "audit_scan",
    },
    "write_community_create_thread": {
        "label": "發表主題",
        "description": "root 專用白名單寫入工具：在指定討論版建立主題。",
        "min_role": "super_admin",
        "data_scope": "write_tool:community",
    },
    "write_community_reply_thread": {
        "label": "回覆主題",
        "description": "root 專用白名單寫入工具：在指定主題留言。",
        "min_role": "super_admin",
        "data_scope": "write_tool:community",
    },
    "write_comfyui_generate": {
        "label": "執行生圖",
        "description": "root 專用白名單寫入工具：送出 ComfyUI 生圖任務。",
        "min_role": "super_admin",
        "data_scope": "write_tool:comfyui",
    },
    "write_chess_create_practice": {
        "label": "建立西洋棋練習",
        "description": "root 專用白名單寫入工具：建立電腦對局練習。",
        "min_role": "super_admin",
        "data_scope": "write_tool:games",
    },
    "write_chess_make_move": {
        "label": "西洋棋走子",
        "description": "root 專用白名單寫入工具：在指定棋局送出一步棋。",
        "min_role": "super_admin",
        "data_scope": "write_tool:games",
    },
    "write_member_create_user": {
        "label": "新增會員",
        "description": "root 專用白名單寫入工具：透過既有會員管理 API 新增帳號。",
        "min_role": "super_admin",
        "data_scope": "write_tool:members",
    },
    "write_member_update_user": {
        "label": "更新會員",
        "description": "root 專用白名單寫入工具：透過既有會員管理 API 更新指定帳號。",
        "min_role": "super_admin",
        "data_scope": "write_tool:members",
    },
    "write_member_set_avatar_from_cloud": {
        "label": "設定會員頭像",
        "description": "root 專用白名單工具：從站內雲端圖片設定頭像，可帶入 AI 判斷的裁切與旋轉。",
        "min_role": "super_admin",
        "data_scope": "write_tool:members",
    },
    "write_bug_report_review": {
        "label": "審核 Bug 回報",
        "description": "root 專用白名單寫入工具：審核 bug report 並可設定獎勵點數。",
        "min_role": "super_admin",
        "data_scope": "write_tool:bug_reports",
    },
    "write_launch_requirements_check": {
        "label": "上線需求檢查",
        "description": "root 專用白名單工具：執行上線前需求檢查。",
        "min_role": "super_admin",
        "data_scope": "write_tool:launch_check",
    },
    "write_launch_logs_verify": {
        "label": "上線 log 鏈驗證",
        "description": "root 專用白名單工具：驗證 server-mode log chain。",
        "min_role": "super_admin",
        "data_scope": "write_tool:launch_check",
    },
    "write_launch_doc_read": {
        "label": "上線文件讀取",
        "description": "root 專用白名單工具：讀取 docs/ 內的上線檢查文件。",
        "min_role": "super_admin",
        "data_scope": "write_tool:launch_check",
    },
    "write_trading_place_order": {"label": "建立交易掛單", "description": "root 專用白名單工具：建立交易訂單。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_cancel_order": {"label": "取消交易掛單", "description": "root 專用白名單工具：取消交易訂單。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_bot_create": {"label": "建立交易機器人", "description": "root 專用白名單工具：建立交易機器人。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_bot_backtest": {"label": "交易機器人回測", "description": "root 專用白名單工具：執行交易策略回測。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_bot_scan": {"label": "執行交易機器人掃描", "description": "root 專用白名單工具：手動掃描交易機器人。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_grid_preview": {"label": "網格交易預覽", "description": "root 專用白名單工具：預覽網格交易。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_grid_bot_create": {"label": "建立網格交易機器人", "description": "root 專用白名單工具：建立網格交易機器人。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_grid_bot_toggle": {"label": "切換網格機器人", "description": "root 專用白名單工具：啟停網格交易機器人。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_margin_open": {"label": "開立槓桿倉位", "description": "root 專用白名單工具：開立槓桿倉位。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_margin_close": {"label": "關閉槓桿倉位", "description": "root 專用白名單工具：關閉槓桿倉位。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_margin_add_collateral": {"label": "追加槓桿保證金", "description": "root 專用白名單工具：追加保證金。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_margin_withdraw_collateral": {"label": "提領槓桿保證金", "description": "root 專用白名單工具：提領保證金。", "min_role": "super_admin", "data_scope": "write_tool:trading"},
    "write_trading_background_run_once": {"label": "執行交易背景任務", "description": "root 專用白名單工具：執行交易背景 engine。", "min_role": "super_admin", "data_scope": "write_tool:trading_root"},
    "write_trading_liquidation_scan": {"label": "掃描借貸清算", "description": "root 專用白名單工具：掃描清算。", "min_role": "super_admin", "data_scope": "write_tool:trading_root"},
    "write_trading_order_match": {"label": "撮合交易訂單", "description": "root 專用白名單工具：撮合交易訂單。", "min_role": "super_admin", "data_scope": "write_tool:trading_root"},
    "write_trading_bot_audit_run": {"label": "交易機器人審計", "description": "root 專用白名單工具：執行交易機器人審計。", "min_role": "super_admin", "data_scope": "write_tool:trading_root"},
    "write_trading_verify_jobs": {"label": "交易記帳驗證", "description": "root 專用白名單工具：觸發交易記帳驗證。", "min_role": "super_admin", "data_scope": "write_tool:trading_root"},
    "write_trading_market_update": {"label": "更新交易市場", "description": "root 專用白名單工具：更新交易市場設定。", "min_role": "super_admin", "data_scope": "write_tool:trading_root"},
    "write_cloud_drive_create_text": {"label": "建立雲端文字檔", "description": "root 專用白名單工具：建立雲端文字檔。", "min_role": "super_admin", "data_scope": "write_tool:cloud_drive"},
    "write_cloud_drive_upload": {"label": "建立雲端文字檔", "description": "root 專用白名單工具：JSON 版建立文字檔。", "min_role": "super_admin", "data_scope": "write_tool:cloud_drive"},
    "write_cloud_drive_delete": {"label": "刪除雲端檔案", "description": "root 專用白名單工具：刪除雲端檔案。", "min_role": "super_admin", "data_scope": "write_tool:cloud_drive"},
    "write_cloud_drive_remote_download": {"label": "建立遠端下載", "description": "root 專用白名單工具：建立遠端下載任務。", "min_role": "super_admin", "data_scope": "write_tool:download"},
    "write_remote_download_direct": {"label": "建立 Direct download", "description": "root 專用白名單工具：建立 Direct download 任務。", "min_role": "super_admin", "data_scope": "write_tool:download"},
    "write_remote_download_bt": {"label": "建立 BT/magnet download", "description": "root 專用白名單工具：建立 BT 或 magnet 任務。", "min_role": "super_admin", "data_scope": "write_tool:download"},
    "write_remote_download_pause": {"label": "暫停遠端下載", "description": "root 專用白名單工具：暫停下載任務。", "min_role": "super_admin", "data_scope": "write_tool:download"},
    "write_remote_download_resume": {"label": "恢復遠端下載", "description": "root 專用白名單工具：恢復下載任務。", "min_role": "super_admin", "data_scope": "write_tool:download"},
    "write_remote_download_cancel": {"label": "取消遠端下載", "description": "root 專用白名單工具：取消下載任務。", "min_role": "super_admin", "data_scope": "write_tool:download"},
    "write_remote_download_recover": {"label": "恢復中斷下載", "description": "root 專用白名單工具：恢復中斷下載。", "min_role": "super_admin", "data_scope": "write_tool:download"},
    "write_share_create": {"label": "建立檔案分享", "description": "root 專用白名單工具：建立檔案分享連結。", "min_role": "super_admin", "data_scope": "write_tool:share"},
    "write_share_update": {"label": "更新分享", "description": "root 專用白名單工具：更新分享設定。", "min_role": "super_admin", "data_scope": "write_tool:share"},
    "write_share_revoke": {"label": "撤銷分享", "description": "root 專用白名單工具：撤銷分享。", "min_role": "super_admin", "data_scope": "write_tool:share"},
    "write_task_cancel": {"label": "取消任務", "description": "root 專用白名單工具：取消 Job Center 任務。", "min_role": "super_admin", "data_scope": "write_tool:jobs"},
    "write_task_retry": {"label": "重試任務", "description": "root 專用白名單工具：重試 Job Center 任務。", "min_role": "super_admin", "data_scope": "write_tool:jobs"},
    "write_automation_job_run": {"label": "重試自動化任務", "description": "root 專用白名單工具：執行可重試自動化任務。", "min_role": "super_admin", "data_scope": "write_tool:jobs"},
    "write_album_create": {"label": "建立相簿", "description": "root 專用白名單工具：建立相簿。", "min_role": "super_admin", "data_scope": "write_tool:albums"},
    "write_album_update": {"label": "更新相簿", "description": "root 專用白名單工具：更新相簿。", "min_role": "super_admin", "data_scope": "write_tool:albums"},
    "write_album_delete": {"label": "刪除相簿", "description": "root 專用白名單工具：刪除相簿。", "min_role": "super_admin", "data_scope": "write_tool:albums"},
    "write_album_add_file": {"label": "加入相簿檔案", "description": "root 專用白名單工具：加入相簿檔案。", "min_role": "super_admin", "data_scope": "write_tool:albums"},
    "write_album_remove_file": {"label": "移除相簿檔案", "description": "root 專用白名單工具：移除相簿檔案。", "min_role": "super_admin", "data_scope": "write_tool:albums"},
    "write_album_smart_organize": {"label": "相簿智慧整理", "description": "root 專用白名單工具：執行相簿智慧整理。", "min_role": "super_admin", "data_scope": "write_tool:albums"},
    "write_video_upload": {"label": "發布既有雲端影音", "description": "root 專用白名單工具：發布既有雲端影音。", "min_role": "super_admin", "data_scope": "write_tool:media"},
    "write_video_publish": {"label": "發布既有雲端影音", "description": "root 專用白名單工具：發布影音。", "min_role": "super_admin", "data_scope": "write_tool:media"},
    "write_video_update": {"label": "更新影音", "description": "root 專用白名單工具：更新影音。", "min_role": "super_admin", "data_scope": "write_tool:media"},
    "write_video_delete": {"label": "刪除影音", "description": "root 專用白名單工具：刪除影音。", "min_role": "super_admin", "data_scope": "write_tool:media"},
    "write_video_streaming_modes": {"label": "更新影音串流模式", "description": "root 專用白名單工具：更新串流模式。", "min_role": "super_admin", "data_scope": "write_tool:media"},
    "write_transcode_hls": {"label": "排程 HLS 轉檔", "description": "root 專用白名單工具：排程 HLS 轉檔。", "min_role": "super_admin", "data_scope": "write_tool:media"},
    "write_hls_rebuild": {"label": "重建 HLS", "description": "root 專用白名單工具：重建 HLS。", "min_role": "super_admin", "data_scope": "write_tool:media"},
    "write_subtitle_upload": {"label": "上傳字幕文字", "description": "root 專用白名單工具：加入字幕文字。", "min_role": "super_admin", "data_scope": "write_tool:media"},
    "write_community_thread_reward": {"label": "獎勵主題作者", "description": "root 專用白名單工具：獎勵主題作者。", "min_role": "super_admin", "data_scope": "write_tool:community"},
    "write_community_post_penalty": {"label": "處罰留言作者", "description": "root 專用白名單工具：處罰違規留言。", "min_role": "super_admin", "data_scope": "write_tool:community"},
    "write_points_governance_execute": {"label": "執行治理提案", "description": "root 專用白名單工具：執行治理提案。", "min_role": "super_admin", "data_scope": "write_tool:governance"},
    "write_points_governance_sponsor": {"label": "贊助治理提案", "description": "root 專用白名單工具：贊助治理提案。", "min_role": "super_admin", "data_scope": "write_tool:governance"},
    "write_points_governance_cancel": {"label": "取消治理提案", "description": "root 專用白名單工具：取消治理提案。", "min_role": "super_admin", "data_scope": "write_tool:governance"},
    "write_points_wallet_freeze_proposal": {"label": "建立錢包凍結治理提案", "description": "root 專用白名單工具：建立錢包凍結治理提案。", "min_role": "super_admin", "data_scope": "write_tool:governance"},
    "write_points_wallet_transfer": {"label": "提交錢包轉帳", "description": "root 專用白名單工具：提交站內點數鏈錢包轉帳交易。", "min_role": "super_admin", "data_scope": "write_tool:points_wallet"},
    "write_server_integrity_repair": {"label": "修復完整性鏈", "description": "root 專用白名單工具：修復完整性鏈。", "min_role": "super_admin", "data_scope": "write_tool:server"},
    "write_server_restart": {"label": "重啟伺服器", "description": "root 專用白名單工具：排程伺服器重啟。", "min_role": "super_admin", "data_scope": "write_tool:server"},
    "write_server_mode_checkpoint": {"label": "建立伺服器模式 checkpoint", "description": "root 專用白名單工具：建立 server-mode checkpoint。", "min_role": "super_admin", "data_scope": "write_tool:server"},
    "write_server_mode_switch": {"label": "切換伺服器模式", "description": "root 專用白名單工具：切換 server-mode。", "min_role": "super_admin", "data_scope": "write_tool:server"},
    "write_incident_enter": {"label": "進入緊急事件模式", "description": "root 專用白名單工具：進入 incident lockdown。", "min_role": "super_admin", "data_scope": "write_tool:server"},
    "write_incident_resolve": {"label": "解除緊急事件模式", "description": "root 專用白名單工具：解除 incident lockdown。", "min_role": "super_admin", "data_scope": "write_tool:server"},
}

AI_AGENT_SAFETY_BOUNDARIES = (
    "不得要求或收集帳號憑證、API key、session token、私密金鑰。",
    "不得輸出可執行指令、程式碼、SQL、腳本或可直接修改伺服器狀態的操作。",
    "不得建議或引導惡意存取、越權、刪除資料與提權流程。",
    "對超出站內導覽、生圖、提示詞、下載排錯範圍的請求，需明確拒絕並給予建議改走站內正規流程。",
)

AI_AGENT_ROLE_SCOPES = {
    "user": {
        "label": "個別用戶助手",
        "description": "專門處理已登入用戶的站內導覽、排錯與提示詞建議，僅提供讀取與建議，不代為操作。",
        "capabilities": [
            "個人任務查詢（生圖 / 下載）",
            "站內流程導覽",
            "提示詞與參數建議",
            "失敗排查步驟建議（只提供指引）",
        ],
    },
    "manager": {
        "label": "管理者助手",
        "description": "除了個別用戶能力外，提供會員管理輔助方向與帳號異常判讀（讀取導向）。",
        "capabilities": [
            "個人任務查詢（生圖 / 下載）",
            "站內流程導覽",
            "提示詞與參數建議",
            "失敗排查步驟建議（只提供指引）",
            "會員管理與帳號狀態（只提供唯讀建議）",
        ],
        "additional_tasks": ["member_management"],
    },
    "super_admin": {
        "label": "最高管理者助手",
        "description": "管理者能力加上伺服器資源、攻擊告警與 root 專用白名單工具協調；實際寫入只在 write 模式、root 身分與確認碼通過後執行。",
        "capabilities": [
            "個人任務查詢（生圖 / 下載）",
            "站內流程導覽",
            "提示詞與參數建議",
            "失敗排查步驟建議（只提供指引）",
            "會員管理與帳號狀態（只提供唯讀建議）",
            "伺服器資源與攻擊訊號（只提供診斷建議）",
            "write 模式下可協助 root 準備白名單 write-tool 操作，並提醒必須經伺服器端點與確認碼。",
        ],
        "additional_tasks": ["member_management", "attack_diagnosis"],
    },
}


class AiAgentError(Exception):
    """Raised when the configured AI Agent backend cannot satisfy a request."""

    def __init__(self, message, *, status=None, payload=None, http_status=None):
        self.status = status
        self.payload = payload
        self.http_status = http_status
        super().__init__(message)


def normalize_ai_agent_api_base_url(value, *, allow_blank=True):
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return "" if allow_blank else None
    if len(raw) > 2048:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.fragment:
        return None
    if parsed.query:
        return None
    return raw


def validate_ai_agent_api_key(value, *, allow_blank=True):
    raw = str(value or "").strip()
    if not raw:
        return "" if allow_blank else None
    if len(raw) > 2048 or any(ch.isspace() for ch in raw):
        return None
    return raw


def normalize_ai_agent_model(value):
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_AI_AGENT_MODEL
    if len(raw) > 200 or any(ch in raw for ch in "\r\n\t"):
        return None
    return raw


def normalize_ai_agent_provider(value):
    raw = str(value or DEFAULT_AI_AGENT_PROVIDER).strip().lower()
    return raw if raw in {"hermes", "openai_compatible"} else None


def normalize_ai_agent_persona(value):
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_AI_AGENT_PERSONA
    return raw if raw in AI_AGENT_PERSONA_PRESETS else None


def _normalize_ai_agent_task_flag(value, *, default=True):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    raw = str(value).strip().lower()
    if raw in {"1", "true", "on", "yes", "y"}:
        return True
    if raw in {"0", "false", "off", "no", "n", "disable", "disabled"}:
        return False
    return default


def _normalize_ai_agent_behavior(settings, *, actor_role="user"):
    persona = normalize_ai_agent_persona(settings.get("ai_agent_persona")) or DEFAULT_AI_AGENT_PERSONA
    tasks = {
        "site_guide": _normalize_ai_agent_task_flag(settings.get("ai_agent_task_site_guide"), default=True),
        "troubleshoot": _normalize_ai_agent_task_flag(settings.get("ai_agent_task_troubleshoot"), default=True),
        "prompt": _normalize_ai_agent_task_flag(settings.get("ai_agent_task_prompt"), default=True),
    }
    tools = ai_agent_effective_tools(settings, actor_role=actor_role)
    return {
        "persona": persona,
        "tasks": tasks,
        "tools": tools,
    }


def normalize_ai_agent_role(value):
    raw = str(value or "").strip().lower()
    if raw in {"admin"}:
        return "manager"
    if raw in AI_AGENT_ROLE_SCOPES:
        return raw
    if raw in {"root", "super", "super_admin"}:
        return "super_admin"
    return "user"


def normalize_ai_agent_actor_role(actor):
    if isinstance(actor, dict):
        username = str(actor.get("username") or "").strip()
        if username == "root":
            return "super_admin"
        return normalize_ai_agent_role(actor.get("role"))
    return normalize_ai_agent_role(actor)


def _agent_role_scope(role):
    return AI_AGENT_ROLE_SCOPES.get(role, AI_AGENT_ROLE_SCOPES["user"])


def _ai_agent_actor_context(actor, actor_role):
    username = ""
    if isinstance(actor, dict):
        username = str(actor.get("username") or "").strip()
    elif actor:
        username = str(actor or "").strip()
    normalized_role = normalize_ai_agent_role(actor_role)
    scope = _agent_role_scope(normalized_role)
    if not username:
        username = "unknown"
    return (
        f"目前登入者：{username}。\n"
        f"目前權限：{normalized_role}（{scope['label']}）。\n"
    )


def _role_allows(required_role, actor_role):
    return AI_AGENT_ROLE_RANK.get(normalize_ai_agent_role(actor_role), 0) >= AI_AGENT_ROLE_RANK.get(normalize_ai_agent_role(required_role), 0)


def ai_agent_effective_tools(settings, *, actor_role="user"):
    configured = normalize_ai_agent_allowed_tools((settings or {}).get("ai_agent_allowed_tools"))
    configured_set = set(configured.split(",")) if configured else set()
    result = []
    for tool_name, details in AI_AGENT_TOOL_BLUEPRINT.items():
        if configured_set and tool_name not in configured_set:
            continue
        if not _role_allows(details.get("min_role") or "user", actor_role):
            continue
        result.append({
            "name": tool_name,
            "label": details["label"],
            "description": details["description"],
            "min_role": details.get("min_role") or "user",
            "data_scope": details.get("data_scope") or "",
        })
    return result


def _ai_agent_system_prompt(behavior, *, role="user", actor=None, allow_tool_runs=False, operation_mode=DEFAULT_AI_AGENT_OPERATION_MODE):
    normalized_role = normalize_ai_agent_role(role)
    scope = _agent_role_scope(normalized_role)
    persona_meta = AI_AGENT_PERSONA_PRESETS.get(behavior.get("persona"), AI_AGENT_PERSONA_PRESETS[DEFAULT_AI_AGENT_PERSONA])
    mode_policy = ai_agent_operation_mode_policy(operation_mode)
    enabled_tasks = [
        f"- {AI_AGENT_TASKS[task_id]['label']}: {AI_AGENT_TASKS[task_id]['description']}"
        for task_id in AI_AGENT_TASKS
        if behavior.get("tasks", {}).get(task_id)
    ]
    disabled_tasks = [
        AI_AGENT_TASKS[task_id]["label"]
        for task_id in AI_AGENT_TASKS
        if not behavior.get("tasks", {}).get(task_id)
    ]
    tool_lines = []
    for detail in behavior.get("tools") or []:
        tool_lines.append(f"- {detail.get('name')}（{detail.get('label')}）：{detail.get('description')}；資料範圍={detail.get('data_scope') or '-'}")
    if mode_policy.get("write_enabled") and normalized_role == "super_admin":
        tool_scope = (
            "目前是 root 專用執行寫入模式：你不是一般使用者助手，也不是唯讀模式。"
            "你可協助 root 準備白名單 write-tool 操作、檢查必要參數與說明風險；"
            "若站內前台已提供對應工具面板（例如 ComfyUI 產圖），請優先引導 root 在前台直接執行，不要要求複製 JSON 或手動 POST；"
            "真正寫入必須透過 /api/ai-agent/write-tools/execute，且同時通過 root 身分、工具白名單、confirm=EXECUTE，以及 write 模式或 root 本次提權確認。"
            "未收到工具端點成功結果前，不得聲稱已完成任何寫入。"
        )
    elif allow_tool_runs:
        tool_scope = "可提供建議型工具摘要；若模式不是 write 或身分不是 root，不得下發站內變更操作。"
    else:
        tool_scope = "工具僅提供可執行建議，不會直接呼叫系統 API 或修改站內狀態。"

    return (
        "你是 hackme_web 網站內的 AI 助理，嚴格負責在本站功能邊界內回答。\n"
        f"角色：{persona_meta['label']}。\n"
        f"語氣：{persona_meta['tone']}。\n"
        f"基本原則：{persona_meta['guidance']}\n"
        f"{_ai_agent_actor_context(actor, normalized_role)}"
        f"服務範圍：{scope['label']}。\n"
        f"用途：{scope['description']}\n"
        f"目前模式：{mode_policy['label']}（{mode_policy['mode']}）。{mode_policy['description']}\n"
        "可執行任務：\n"
        + "\n".join(enabled_tasks or ["- 目前未啟用任務，請管理端先啟用任務後再處理。"]) + "\n"
        "可提供服務：\n"
        + "\n".join(f"- {item}" for item in scope["capabilities"]) + "\n"
        + "安全邊際：\n"
        + "\n".join(f"- {item}" for item in AI_AGENT_SAFETY_BOUNDARIES) + "\n"
        + "工具公告：\n"
        + "\n".join(tool_lines) + "\n"
        f"{tool_scope}\n"
        "工具鐵則：你不能在一般聊天中聲稱已呼叫、正在呼叫或已完成任何工具/API。"
        "禁止輸出「工具：check_generation_progress」、「送出產圖任務」、「執行寫入中」等假工具狀態。"
        "若需要工具，請說明需要由前台工具流程處理或等待實際工具結果；只有系統/前端回傳工具結果後才能回報狀態。\n"
        + (f"未啟用任務提示：{', '.join(disabled_tasks)}\n" if disabled_tasks else "")
        + "回應時若使用者需求不在可執行任務範圍，請明確回應無法執行並引導到可用功能。\n"
    )


def _coerce_audit_settings(settings):
    settings = settings or {}
    return {
        "operation_mode": normalize_ai_agent_operation_mode(settings.get("ai_agent_operation_mode")) or DEFAULT_AI_AGENT_OPERATION_MODE,
        "allowed_models": normalize_ai_agent_allowed_models(settings.get("ai_agent_allowed_models")) or "",
        "audit_interval_minutes": normalize_ai_agent_audit_interval_minutes(
            settings.get("ai_agent_audit_interval_minutes"),
            default=AI_AGENT_AUDIT_INTERVAL_MINUTES_DEFAULT,
        ),
        "audit_cpu_percent_threshold": normalize_ai_agent_audit_int(
            settings.get("ai_agent_audit_cpu_percent_threshold"),
            "ai_agent_audit_cpu_percent_threshold",
            default=AI_AGENT_AUDIT_CPU_PERCENT_THRESHOLD_DEFAULT,
            minimum=10,
            maximum=100,
        ),
        "audit_ram_percent_threshold": normalize_ai_agent_audit_int(
            settings.get("ai_agent_audit_ram_percent_threshold"),
            "ai_agent_audit_ram_percent_threshold",
            default=AI_AGENT_AUDIT_RAM_PERCENT_THRESHOLD_DEFAULT,
            minimum=10,
            maximum=100,
        ),
        "audit_disk_percent_threshold": normalize_ai_agent_audit_int(
            settings.get("ai_agent_audit_disk_percent_threshold"),
            "ai_agent_audit_disk_percent_threshold",
            default=AI_AGENT_AUDIT_DISK_PERCENT_THRESHOLD_DEFAULT,
            minimum=10,
            maximum=100,
        ),
        "audit_ip_event_rate_threshold": normalize_ai_agent_audit_int(
            settings.get("ai_agent_audit_ip_event_rate_threshold"),
            "ai_agent_audit_ip_event_rate_threshold",
            default=AI_AGENT_AUDIT_IP_EVENT_RATE_THRESHOLD_DEFAULT,
            minimum=1,
            maximum=10000,
        ),
        "audit_ip_event_rate_window_minutes": normalize_ai_agent_audit_int(
            settings.get("ai_agent_audit_ip_event_rate_window_minutes"),
            "ai_agent_audit_ip_event_rate_window_minutes",
            default=AI_AGENT_AUDIT_IP_EVENT_RATE_WINDOW_MINUTES_DEFAULT,
            minimum=1,
            maximum=1440,
        ),
        "audit_security_event_rate_threshold": normalize_ai_agent_audit_int(
            settings.get("ai_agent_audit_security_event_rate_threshold"),
            "ai_agent_audit_security_event_rate_threshold",
            default=AI_AGENT_AUDIT_SECURITY_EVENT_RATE_THRESHOLD_DEFAULT,
            minimum=1,
            maximum=10000,
        ),
        "audit_security_event_rate_window_minutes": normalize_ai_agent_audit_int(
            settings.get("ai_agent_audit_security_event_rate_window_minutes"),
            "ai_agent_audit_security_event_rate_window_minutes",
            default=AI_AGENT_AUDIT_SECURITY_EVENT_RATE_WINDOW_MINUTES_DEFAULT,
            minimum=1,
            maximum=1440,
        ),
        "audit_auto_block_suspect_ip": normalize_ai_agent_audit_bool(
            settings.get("ai_agent_audit_auto_block_suspect_ip"),
            default=AI_AGENT_AUDIT_AUTO_BLOCK_DEFAULT,
        ),
        "audit_block_minutes": normalize_ai_agent_audit_int(
            settings.get("ai_agent_audit_block_minutes"),
            "ai_agent_audit_block_minutes",
            default=AI_AGENT_AUDIT_BLOCK_MINUTES_DEFAULT,
            minimum=1,
            maximum=60 * 24 * 7,
        ),
        "audit_notify_root": normalize_ai_agent_audit_bool(
            settings.get("ai_agent_audit_notify_root"),
            default=AI_AGENT_AUDIT_NOTIFY_ROOT_DEFAULT,
        ),
    }


def _safe_audit_timestamp(value, *, default_minutes=5):
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.isoformat()
    except Exception:
        return (datetime.now() - timedelta(minutes=default_minutes)).isoformat()


def _safe_parse_iso(value):
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _extract_text_from_messages(messages):
    pieces = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            pieces.append(content)
            continue
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    pieces.append(str(part.get("text") or ""))
    return "\n".join(pieces)


def _contains_audit_mode_prohibited_action(text):
    sample = str(text or "").lower()
    if not sample:
        return False
    blocked = (
        "刪除",
        "清除",
        "移除",
        "修改",
        "改變",
        "更新",
        "上傳",
        "刪掉",
        "封鎖",
        "封鎖",
        "擋",
        "封掉",
        "block",
        "delete",
        "remove",
        "restart",
        "kill",
        "shutdown",
        "關閉",
        "開啟",
        "start",
        "stop",
    )
    for token in blocked:
        if token in sample:
            return True
    return False


def _table_exists(conn, table_name):
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _safe_rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return []


def _row_get(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        pass
    try:
        return dict(row).get(key, default)
    except Exception:
        return default


def public_ai_agent_settings(settings, *, actor=None):
    settings = settings or {}
    key = str(settings.get("ai_agent_api_key") or "").strip()
    actor_role = normalize_ai_agent_actor_role(actor if actor is not None else "user")
    behavior = _normalize_ai_agent_behavior(settings, actor_role=actor_role)
    audit_settings = _coerce_audit_settings(settings)
    mode_policy = ai_agent_operation_mode_policy(audit_settings["operation_mode"])
    return {
        "provider": normalize_ai_agent_provider(settings.get("ai_agent_provider")) or DEFAULT_AI_AGENT_PROVIDER,
        "api_base_url": normalize_ai_agent_api_base_url(
            settings.get("ai_agent_api_base_url") or DEFAULT_AI_AGENT_API_BASE_URL,
            allow_blank=True,
        ) or "",
        "api_key_configured": bool(key),
        "model": normalize_ai_agent_model(settings.get("ai_agent_model")) or DEFAULT_AI_AGENT_MODEL,
        "request_timeout_seconds": parse_int_setting(settings, "ai_agent_request_timeout_seconds", 120, 5, 600),
        "max_prompt_chars": parse_int_setting(settings, "ai_agent_max_prompt_chars", 80000, 1000, 200000),
        "allow_image_input": bool(settings.get("ai_agent_allow_image_input", True)),
        "allow_tool_runs": bool(settings.get("ai_agent_allow_tool_runs", False)),
        "operation_mode": audit_settings["operation_mode"],
        "operation_mode_policy": mode_policy,
        "allowed_models": audit_settings["allowed_models"],
        "allowed_tools": normalize_ai_agent_allowed_tools(settings.get("ai_agent_allowed_tools")) or "",
        "audit_interval_minutes": audit_settings["audit_interval_minutes"],
        "audit_thresholds": {
            "cpu_percent": audit_settings["audit_cpu_percent_threshold"],
            "ram_percent": audit_settings["audit_ram_percent_threshold"],
            "disk_percent": audit_settings["audit_disk_percent_threshold"],
            "ip_event_rate_per_min": audit_settings["audit_ip_event_rate_threshold"],
            "ip_event_rate_window_minutes": audit_settings["audit_ip_event_rate_window_minutes"],
            "security_event_rate_per_min": audit_settings["audit_security_event_rate_threshold"],
            "security_event_rate_window_minutes": audit_settings["audit_security_event_rate_window_minutes"],
            "auto_block_suspect_ip": audit_settings["audit_auto_block_suspect_ip"],
            "auto_block_minutes": audit_settings["audit_block_minutes"],
            "notify_root": audit_settings["audit_notify_root"],
        },
        "role": actor_role,
        "scope": _agent_role_scope(actor_role),
        "safety_boundaries": list(AI_AGENT_SAFETY_BOUNDARIES),
        "persona": behavior["persona"],
        "tasks": behavior["tasks"],
        "tools": behavior["tools"],
    }


def _backend_base_url(settings):
    base_url = normalize_ai_agent_api_base_url(
        (settings or {}).get("ai_agent_api_base_url") or DEFAULT_AI_AGENT_API_BASE_URL,
        allow_blank=False,
    )
    if not base_url:
        raise AiAgentError("AI Agent API 位址尚未設定或格式錯誤")
    return base_url


def _backend_timeout(settings):
    return parse_int_setting(settings, "ai_agent_request_timeout_seconds", 120, 5, 600)


def _backend_headers(settings, *, session_key=""):
    headers = {"Content-Type": "application/json"}
    api_key = validate_ai_agent_api_key((settings or {}).get("ai_agent_api_key"), allow_blank=True) or ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        if session_key:
            headers["X-Hermes-Session-Key"] = str(session_key)[:240]
    return headers


def _json_request(settings, method, path, payload=None, *, session_key="", timeout=None):
    base_url = _backend_base_url(settings)
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib_request.Request(
        url,
        data=body,
        headers=_backend_headers(settings, session_key=session_key),
        method=method.upper(),
    )
    try:
        with urllib_request.urlopen(req, timeout=timeout or _backend_timeout(settings)) as resp:
            raw = resp.read(10 * 1024 * 1024)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
    except urllib_error.HTTPError as exc:
        raw = exc.read(512 * 1024)
        payload = {}
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            payload = {"raw": raw.decode("utf-8", "replace")}
        message = (
            payload.get("error", {}).get("message")
            if isinstance(payload.get("error"), dict)
            else payload.get("msg") or payload.get("message")
        )
        raise AiAgentError(message or f"AI Agent backend HTTP {exc.code}", status=exc.code, payload=payload)
    except (urllib_error.URLError, TimeoutError, OSError) as exc:
        raise AiAgentError(f"AI Agent backend 無法連線：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise AiAgentError(f"AI Agent backend 回傳不是有效 JSON：{exc}") from exc


def _safe_percent(value):
    try:
        number = float(value)
        if number != number:
            return None
        return max(0.0, min(100.0, number))
    except Exception:
        return None


def _read_meminfo_int(key):
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as stream:
            for line in stream:
                if not line.startswith(f"{key}:"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except Exception:
        return None
    return None


def _snapshot_resources():
    cores = os.cpu_count() or 1
    try:
        load_avg = list(os.getloadavg())
    except Exception:
        load_avg = None
    total_ram = _read_meminfo_int("MemTotal")
    available_ram = _read_meminfo_int("MemAvailable")
    if total_ram is None or available_ram is None:
        ram_percent = None
    else:
        used_ram = max(0, total_ram - available_ram)
        ram_percent = _safe_percent((used_ram / float(total_ram)) * 100.0)
    try:
        disk = shutil.disk_usage(".")
        disk_percent = _safe_percent((disk.used / max(1, disk.total)) * 100.0)
    except Exception:
        disk = None
        disk_percent = None
    cpu_percent = None
    if load_avg:
        cpu_percent = _safe_percent((float(load_avg[0]) / max(1, cores)) * 100.0)
    return {
        "sampled_at": datetime.now().replace(microsecond=0).isoformat(),
        "cpu": {
            "cores": cores,
            "percent": cpu_percent,
            "load_avg": load_avg,
        },
        "ram": {
            "total": total_ram or 0,
            "available": available_ram or 0,
            "percent": ram_percent,
        },
        "disk": {
            "total": disk.total if disk else 0,
            "used": disk.used if disk else 0,
            "free": disk.free if disk else 0,
            "percent": disk_percent,
        },
    }


def _read_proc_net_dev():
    total = {
        "sampled_at": datetime.now().replace(microsecond=0).isoformat(),
        "raw_delta_seconds": 0.0,
        "interfaces": {},
        "total_rx_bytes": 0,
        "total_tx_bytes": 0,
    }
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as stream:
            lines = stream.readlines()
    except Exception:
        return total

    for line in lines:
        if ":" not in line:
            continue
        left, right = line.split(":", 1)
        iface = left.strip()
        if not iface or iface.lower() == "lo":
            continue
        parts = right.split()
        if len(parts) < 16:
            continue
        try:
            rx_bytes = int(parts[0])
            tx_bytes = int(parts[8])
            rx_packets = int(parts[1])
            tx_packets = int(parts[9])
        except Exception:
            continue
        total["interfaces"][iface] = {
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "rx_packets": rx_packets,
            "tx_packets": tx_packets,
        }
        total["total_rx_bytes"] += max(0, rx_bytes)
        total["total_tx_bytes"] += max(0, tx_bytes)
    return total


def _snapshot_network_delta():
    now = _read_proc_net_dev()
    state = _AUDIT_SCAN_STATE.get("network_last", {})
    prev = state.get("data", {})
    prev_at = state.get("at", 0.0)
    now_ts = time()
    delta = {
        "sampled_at": now["sampled_at"],
        "window_seconds": 0.0,
        "interfaces": {},
        "total_rx_bytes_delta": 0,
        "total_tx_bytes_delta": 0,
        "total_kib_per_s": 0.0,
    }
    if not prev:
        _AUDIT_SCAN_STATE["network_last"] = {"at": now_ts, "data": now}
        return delta

    prev_ts = time()
    try:
        prev_ts = datetime.fromisoformat(prev.get("sampled_at", now["sampled_at"])).timestamp()
    except Exception:
        prev_ts = prev_at
    window = max(1.0, now_ts - prev_ts)
    delta["window_seconds"] = window

    prev_by_iface = prev.get("interfaces", {})
    for iface, iface_data in now.get("interfaces", {}).items():
        prior = prev_by_iface.get(iface, {})
        rx = max(0, int(iface_data.get("rx_bytes", 0)) - int(prior.get("rx_bytes", 0)))
        tx = max(0, int(iface_data.get("tx_bytes", 0)) - int(prior.get("tx_bytes", 0)))
        if rx < 0 or tx < 0:
            rx = max(0, rx)
            tx = max(0, tx)
        kib_per_s = (rx + tx) / max(1.0, window) / 1024
        if kib_per_s >= 1:
            delta["interfaces"][iface] = {
                "rx_delta": rx,
                "tx_delta": tx,
                "kib_per_s": round(kib_per_s, 3),
            }
            delta["total_rx_bytes_delta"] += rx
            delta["total_tx_bytes_delta"] += tx

    total_bytes = delta["total_rx_bytes_delta"] + delta["total_tx_bytes_delta"]
    delta["total_kib_per_s"] = round((total_bytes / max(1.0, window)) / 1024, 3)
    _AUDIT_SCAN_STATE["network_last"] = {"at": now_ts, "data": now}
    return delta


def _collect_security_samples(conn, *, since_iso):
    result = {
        "security_events_total": 0,
        "security_events": [],
        "secure_audit_total": 0,
        "secure_audit": [],
    }
    if _table_exists(conn, "security_events"):
        result["security_events"] = _safe_rows(
            conn,
            "SELECT event_type, ip_address, target_user, detail, created_at "
            "FROM security_events WHERE created_at>=? ORDER BY id DESC LIMIT 2000",
            (since_iso,),
        )
        result["security_events_total"] = len(result["security_events"])
    if _table_exists(conn, "secure_audit"):
        result["secure_audit"] = _safe_rows(
            conn,
            "SELECT ts, action, ip, user, success, detail FROM secure_audit WHERE ts>=? ORDER BY id DESC LIMIT 2000",
            (since_iso,),
        )
        result["secure_audit_total"] = len(result["secure_audit"])
    return result


def run_ai_agent_audit_scan(settings, *, get_db, actor=None, force=False, get_client_ip=None, get_ua=None, audit=None):
    settings = settings or {}
    actor = actor or {}
    audit_settings = _coerce_audit_settings(settings)
    interval_seconds = max(60, int(audit_settings["audit_interval_minutes"]) * 60)
    now_ts = time()

    with _AUDIT_SCAN_LOCK:
        cached = _AUDIT_SCAN_STATE.get("audit", {})
        last_at = float(cached.get("at") or 0.0)
        if (not force) and last_at and (now_ts - last_at) < interval_seconds and cached.get("data"):
            cached_payload = dict(cached["data"])
            cached_payload["cached"] = True
            cached_payload["cache_expires_at"] = datetime.fromtimestamp(last_at + interval_seconds).replace(microsecond=0).isoformat()
            return cached_payload

    actor_name = str((actor or {}).get("username") or "").strip()
    actor_role = normalize_ai_agent_role((actor or {}).get("role") if isinstance(actor, dict) else "user")

    ip_window_minutes = int(audit_settings["audit_ip_event_rate_window_minutes"])
    security_window_minutes = int(audit_settings["audit_security_event_rate_window_minutes"])
    window_minutes = max(ip_window_minutes, security_window_minutes)

    scan_started = datetime.now().replace(microsecond=0).isoformat()
    since_iso = _safe_audit_timestamp(datetime.now() - timedelta(minutes=window_minutes), default_minutes=window_minutes)
    ip_window_start = _safe_parse_iso(scan_started) - timedelta(minutes=ip_window_minutes)
    security_window_start = _safe_parse_iso(scan_started) - timedelta(minutes=security_window_minutes)

    conn = get_db()
    try:
        samples = _collect_security_samples(conn, since_iso=since_iso)
    finally:
        conn.close()

    security_rows = []
    for row in samples.get("security_events", []):
        sample_ts = _safe_parse_iso(_row_get(row, "created_at"))
        if sample_ts is None:
            continue
        if security_window_start is None or sample_ts >= security_window_start:
            security_rows.append(row)

    request_rows = []
    for row in samples.get("secure_audit", []):
        sample_ts = _safe_parse_iso(_row_get(row, "ts"))
        if sample_ts is None:
            continue
        if ip_window_start is None or sample_ts >= ip_window_start:
            request_rows.append(row)

    resource_snapshot = _snapshot_resources()
    network_delta = _snapshot_network_delta()

    security_type_counts = Counter()
    security_ip_counts = Counter()
    request_ip_counts = Counter()
    request_action_counts = Counter()

    for row in security_rows:
        event_type = str(_row_get(row, "event_type") or "unknown").strip()
        ip = str(_row_get(row, "ip_address") or "").strip()
        security_type_counts[event_type] += 1
        if ip and ip != "-":
            security_ip_counts[ip] += 1

    for row in request_rows:
        ip = str(_row_get(row, "ip") or "").strip()
        action = str(_row_get(row, "action") or "request").strip()
        request_ip_counts[ip] += 1 if ip and ip != "-" else 0
        request_action_counts[action] += 1

    top_request_ips = [{"ip": ip, "count": count} for ip, count in request_ip_counts.most_common(5)]
    top_security_ips = [{"ip": ip, "count": count} for ip, count in security_ip_counts.most_common(5)]
    top_security_event_types = [{"type": key, "count": count} for key, count in security_type_counts.most_common(20)]

    anomalies = []
    recommendations = []
    interventions = []
    notifications = []

    def add_anomaly(code, severity, message, details):
        anomalies.append({
            "code": code,
            "severity": severity,
            "message": message,
            "details": details or {},
        })

    cpu_percent = resource_snapshot["cpu"].get("percent")
    ram_percent = resource_snapshot["ram"].get("percent")
    disk_percent = resource_snapshot["disk"].get("percent")

    if cpu_percent is not None and cpu_percent >= audit_settings["audit_cpu_percent_threshold"]:
        sev = "alert" if cpu_percent >= 100 else "warn"
        add_anomaly(
            "resource.cpu.threshold_exceeded",
            sev,
            f"CPU 使用率偏高：{cpu_percent:.1f}%（阈值 {audit_settings['audit_cpu_percent_threshold']}%）",
            {"cpu_percent": cpu_percent, "threshold": audit_settings["audit_cpu_percent_threshold"]},
        )
        recommendations.append("請檢查 ComfyUI 任務佔用、下載高併發或長時間 blocking 佇列。")
    if ram_percent is not None and ram_percent >= audit_settings["audit_ram_percent_threshold"]:
        sev = "alert" if ram_percent >= 100 else "warn"
        add_anomaly(
            "resource.ram.threshold_exceeded",
            sev,
            f"記憶體使用率偏高：{ram_percent:.1f}%（阈值 {audit_settings['audit_ram_percent_threshold']}%）",
            {"ram_percent": ram_percent, "threshold": audit_settings["audit_ram_percent_threshold"]},
        )
        recommendations.append("請檢查長留任 job、模型加載與下載暫存清理是否正常。")
    if disk_percent is not None and disk_percent >= audit_settings["audit_disk_percent_threshold"]:
        sev = "alert" if disk_percent >= 100 else "warn"
        add_anomaly(
            "resource.disk.threshold_exceeded",
            sev,
            f"磁碟使用率偏高：{disk_percent:.1f}%（阈值 {audit_settings['audit_disk_percent_threshold']}%）",
            {"disk_percent": disk_percent, "threshold": audit_settings["audit_disk_percent_threshold"]},
        )
        recommendations.append("請檢查臨時輸出、日誌與下載殘留檔是否需要清理。")

    ip_threshold = int(audit_settings["audit_ip_event_rate_threshold"])
    ip_rate_window = int(audit_settings["audit_ip_event_rate_window_minutes"])
    ip_threshold_count = max(1, int(ip_threshold * ip_rate_window))
    for item in top_request_ips:
        if item["count"] >= ip_threshold_count:
            rate = item["count"] / max(1, ip_rate_window)
            sev = "alert" if item["count"] >= ip_threshold_count * 2 else "warn"
            add_anomaly(
                "security.request_rate_per_ip",
                sev,
                f"IP {item['ip']} 在 {ip_rate_window} 分鐘請求筆數偏高（{item['count']}）",
                {"ip": item["ip"], "count": item["count"], "window_minutes": ip_rate_window},
            )
            if audit_settings["audit_auto_block_suspect_ip"]:
                try:
                    from services.security.events import block_ip

                    block_ip(item["ip"], minutes=int(audit_settings["audit_block_minutes"]), reason="AI Agent 審計異常請求")
                    interventions.append({
                        "type": "block_ip",
                        "ip": item["ip"],
                        "minutes": int(audit_settings["audit_block_minutes"]),
                        "status": "success",
                        "reason": "請求速率異常",
                    })
                    if audit:
                        audit("AI_AGENT_AUDIT_BLOCK_IP", get_client_ip() if callable(get_client_ip) else "-", actor_name, ua=get_ua() if callable(get_ua) else "", detail=f"ip={item['ip']} count={item['count']}")
                except Exception as exc:
                    interventions.append({
                        "type": "block_ip",
                        "ip": item["ip"],
                        "minutes": int(audit_settings["audit_block_minutes"]),
                        "status": "failed",
                        "reason": str(exc),
                    })
            recommendations.append(f"考慮限流或封鎖來源 IP {item['ip']}。")

    security_threshold = int(audit_settings["audit_security_event_rate_threshold"])
    security_rate_window = int(audit_settings["audit_security_event_rate_window_minutes"])
    security_event_count = len(security_rows)
    security_threshold_count = max(1, int(security_threshold * security_rate_window))
    if security_event_count >= security_threshold_count:
        sev = "alert" if security_event_count >= security_threshold_count * 2 else "warn"
        add_anomaly(
            "security.security_event_rate",
            sev,
            f"近期安全事件偏多（{security_event_count}）",
            {"count": security_event_count, "threshold": security_threshold, "window_minutes": security_rate_window},
        )
        recommendations.append("請檢視 security_events、login/fail 及 rate_limit 類型事件的來源IP是否異常。")

    if network_delta.get("total_kib_per_s", 0.0) >= 512000:
        add_anomaly(
            "network.traffic_spike",
            "warn",
            f"網路傳輸速率較高：{network_delta.get('total_kib_per_s', 0)} KB/s",
            {
                "total_kib_per_s": network_delta.get("total_kib_per_s", 0),
                "window_seconds": network_delta.get("window_seconds", 0),
            },
        )
        recommendations.append("請檢查是否有大量下載/大檔輸出或流量放大來源。")

    status = "ok"
    for item in anomalies:
        if item["severity"] == "alert":
            status = "alert"
            break
    if status != "alert" and any(item["severity"] == "warn" for item in anomalies):
        status = "warn"

    if audit_settings["audit_notify_root"] and status != "ok":
        notifications.append({
            "target": "root",
            "level": status,
            "message": "AI Agent 審計發現異常",
            "details": {
                "anomaly_count": len(anomalies),
                "status": status,
            },
        })

    result = {
        "status": status,
        "scanned_at": scan_started,
        "cached": False,
        "actor": {
            "id": int((actor or {}).get("id") or 0),
            "username": actor_name,
            "role": actor_role,
        },
        "settings": {
            "operation_mode": audit_settings["operation_mode"],
            "allowed_models": audit_settings["allowed_models"],
            "audit_interval_minutes": audit_settings["audit_interval_minutes"],
            "audit_thresholds": {
                "ip_event_rate_threshold_per_min": audit_settings["audit_ip_event_rate_threshold"],
                "ip_event_rate_window_minutes": audit_settings["audit_ip_event_rate_window_minutes"],
                "security_event_rate_threshold_per_min": audit_settings["audit_security_event_rate_threshold"],
                "security_event_rate_window_minutes": audit_settings["audit_security_event_rate_window_minutes"],
                "resource_cpu_threshold": audit_settings["audit_cpu_percent_threshold"],
                "resource_ram_threshold": audit_settings["audit_ram_percent_threshold"],
                "resource_disk_threshold": audit_settings["audit_disk_percent_threshold"],
                "auto_block_suspect_ip": audit_settings["audit_auto_block_suspect_ip"],
                "auto_block_minutes": audit_settings["audit_block_minutes"],
                "notify_root": audit_settings["audit_notify_root"],
            },
        },
        "window": {
            "minutes": window_minutes,
            "start_at": since_iso,
            "end_at": scan_started,
        },
        "resources": resource_snapshot,
        "network": network_delta,
        "aggregates": {
            "security_events_total": samples["security_events_total"],
            "secure_audit_total": samples["secure_audit_total"],
            "security_event_types": top_security_event_types,
            "request_ips": top_request_ips,
            "security_ips": top_security_ips,
            "request_actions": [{"action": action, "count": count} for action, count in request_action_counts.most_common(10)],
        },
        "anomalies": anomalies,
        "interventions": interventions,
        "recommendations": recommendations,
        "notifications": notifications,
        "raw": {
            "sample_count": samples["security_events_total"] + samples["secure_audit_total"],
        },
    }

    with _AUDIT_SCAN_LOCK:
        _AUDIT_SCAN_STATE["audit"] = {
            "at": now_ts,
            "data": result,
        }
    return result


def ai_agent_health(settings):
    provider = normalize_ai_agent_provider((settings or {}).get("ai_agent_provider")) or DEFAULT_AI_AGENT_PROVIDER
    if provider == "openai_compatible":
        try:
            payload = _json_request(settings, "GET", "/models", timeout=min(_backend_timeout(settings), 8))
            return {"ok": True, "url": urljoin(_backend_base_url(settings).rstrip("/") + "/", "models"), "payload": payload}
        except AiAgentError as exc:
            return {"ok": False, "url": urljoin(_backend_base_url(settings).rstrip("/") + "/", "models"), "msg": str(exc), "status": exc.status, "payload": exc.payload}

    base_url = _backend_base_url(settings)
    parsed = urlparse(base_url)
    path = (parsed.path or "").rstrip("/")
    urls = []
    if path:
        urls.append(f"{parsed.scheme}://{parsed.netloc}{path}/health")
    urls.append(f"{parsed.scheme}://{parsed.netloc}/health")

    last_error = ""
    for health_url in urls:
        req = urllib_request.Request(health_url, headers=_backend_headers(settings), method="GET")
        try:
            with urllib_request.urlopen(req, timeout=min(_backend_timeout(settings), 8)) as resp:
                raw = resp.read(1024 * 1024)
                payload = json.loads(raw.decode("utf-8")) if raw else {}
                service = ""
                if isinstance(payload, dict):
                    service = str(payload.get("service") or "").strip().lower()
                if service == "hermes-mock":
                    return {
                        "ok": False,
                        "url": health_url,
                        "msg": "偵測到 hermes-mock 後端，請改連到真實 AI Agent 服務",
                        "payload": payload,
                    }
                return {"ok": True, "url": health_url, "payload": payload}
        except Exception as exc:  # pragma: no cover - fallback path probing
            last_error = str(exc)
            continue

    return {"ok": False, "url": urls[-1] if urls else base_url, "msg": last_error}


def get_ai_agent_audit_last_scan():
    with _AUDIT_SCAN_LOCK:
        last = _AUDIT_SCAN_STATE.get("audit", {})
        payload = last.get("data")
        if isinstance(payload, dict):
            payload = dict(payload)
        else:
            payload = {}
        return {
            "last_scanned_at_ts": float(last.get("at") or 0.0),
            "has_result": bool(payload),
            "scan": payload,
        }


def _safe_datetime_from_timestamp(ts):
    try:
        return datetime.fromtimestamp(float(ts)).replace(microsecond=0).isoformat()
    except Exception:
        return ""


def public_ai_agent_audit_status(settings, *, include_scan=False):
    settings = settings or {}
    audit_settings = _coerce_audit_settings(settings)
    interval_minutes = int(audit_settings["audit_interval_minutes"])
    last_scan = get_ai_agent_audit_last_scan()
    at_ts = float(last_scan.get("last_scanned_at_ts") or 0.0)
    next_due_ts = at_ts + max(1.0, interval_minutes * 60.0) if at_ts > 0 else 0.0
    summary = {}
    scan = last_scan.get("scan") or {}
    if scan:
        summary = {
            "status": scan.get("status") or "unknown",
            "scanned_at": scan.get("scanned_at"),
            "anomaly_count": len(scan.get("anomalies") or []),
            "intervention_count": len(scan.get("interventions") or []),
            "notification_count": len(scan.get("notifications") or []),
            "cache_expires_at": scan.get("cache_expires_at"),
        }
    result = {
        "mode": audit_settings["operation_mode"],
        "scheduler": {
            "enabled": audit_settings["operation_mode"] == "audit",
            "interval_minutes": interval_minutes,
            "last_scanned_at": _safe_datetime_from_timestamp(at_ts),
            "next_due_at": _safe_datetime_from_timestamp(next_due_ts),
            "has_scan": last_scan.get("has_result"),
        },
        "summary": summary,
        "settings": {
            "audit_thresholds": {
                "cpu_percent": audit_settings["audit_cpu_percent_threshold"],
                "ram_percent": audit_settings["audit_ram_percent_threshold"],
                "disk_percent": audit_settings["audit_disk_percent_threshold"],
                "ip_event_rate_threshold_per_min": audit_settings["audit_ip_event_rate_threshold"],
                "ip_event_rate_window_minutes": audit_settings["audit_ip_event_rate_window_minutes"],
                "security_event_rate_threshold_per_min": audit_settings["audit_security_event_rate_threshold"],
                "security_event_rate_window_minutes": audit_settings["audit_security_event_rate_window_minutes"],
                "auto_block_suspect_ip": audit_settings["audit_auto_block_suspect_ip"],
                "auto_block_minutes": audit_settings["audit_block_minutes"],
                "notify_root": audit_settings["audit_notify_root"],
            }
        },
    }
    if include_scan:
        result["scan"] = scan
    return result


def ai_agent_capabilities(settings):
    provider = normalize_ai_agent_provider((settings or {}).get("ai_agent_provider")) or DEFAULT_AI_AGENT_PROVIDER
    if provider == "openai_compatible":
        return {
            "ok": True,
            "provider": provider,
            "chat": True,
            "models": True,
            "capabilities_endpoint": False,
            "tools": [],
        }
    try:
        return _json_request(settings, "GET", "/capabilities", timeout=min(_backend_timeout(settings), 8))
    except AiAgentError as exc:
        return {"ok": False, "msg": str(exc), "status": exc.status}


def ai_agent_models(settings):
    return _json_request(settings, "GET", "/models", timeout=min(_backend_timeout(settings), 15))


def _message_text_length(messages):
    total = 0
    for message in messages or []:
        content = message.get("content") if isinstance(message, dict) else ""
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(str(part.get("text") or ""))
    return total


def _trim_chat_history_to_budget(messages, max_chars):
    """Keep the newest chat turns when accumulated history exceeds the prompt budget."""
    if _message_text_length(messages) <= max_chars:
        return list(messages or [])
    kept = []
    total = 0
    for message in reversed(list(messages or [])):
        message_len = _message_text_length([message])
        if not kept and message_len > max_chars:
            return [message]
        if total + message_len <= max_chars:
            kept.append(message)
            total += message_len
    return list(reversed(kept))


def _normalize_chat_messages(messages, *, prompt="", image_data_url="", allow_image_input=True):
    normalized = []
    source = messages if isinstance(messages, list) else []
    for item in source:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower()
        if role not in {"system", "user", "assistant", "tool"}:
            role = "user"
        content = item.get("content")
        if isinstance(content, str):
            normalized.append({"role": role, "content": content[:200000]})
        elif isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                part_type = str(part.get("type") or "").strip()
                if part_type == "text":
                    parts.append({"type": "text", "text": str(part.get("text") or "")[:200000]})
                elif part_type == "image_url" and allow_image_input:
                    image_url = part.get("image_url") or {}
                    if isinstance(image_url, dict):
                        url = str(image_url.get("url") or "")
                    else:
                        url = str(image_url or "")
                    if url.startswith("data:image/") and len(url) <= MAX_AI_AGENT_IMAGE_DATA_URL_CHARS:
                        parts.append({"type": "image_url", "image_url": {"url": url}})
            if parts:
                normalized.append({"role": role, "content": parts})
    if not normalized and prompt:
        normalized.append({"role": "user", "content": str(prompt)})
    if image_data_url:
        if not allow_image_input:
            raise AiAgentError("目前設定不允許圖片輸入", http_status=403)
        image_data_url = str(image_data_url or "")
        if not image_data_url.startswith("data:image/") or len(image_data_url) > MAX_AI_AGENT_IMAGE_DATA_URL_CHARS:
            raise AiAgentError("圖片資料格式錯誤或超過大小限制", http_status=400)
        if not normalized:
            normalized.append({"role": "user", "content": []})
        last = normalized[-1]
        content = last.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        elif not isinstance(content, list):
            content = []
        content.append({"type": "image_url", "image_url": {"url": image_data_url}})
        last["content"] = content
    return normalized


def ai_agent_chat(settings, *, messages=None, prompt="", image_data_url="", model="", session_key="", actor=None):
    public = public_ai_agent_settings(settings, actor=actor)
    normalized_messages = _normalize_chat_messages(
        messages,
        prompt=prompt,
        image_data_url=image_data_url,
        allow_image_input=public["allow_image_input"],
    )
    if not normalized_messages:
        raise AiAgentError("請輸入訊息", http_status=400)

    sanitized_messages = [
        message
        for message in normalized_messages
        if str(message.get("role") or "").strip() in {"user", "assistant"}
    ]
    if not sanitized_messages:
        raise AiAgentError("請輸入訊息", http_status=400)
    actor_role = normalize_ai_agent_actor_role(actor if actor is not None else "user")
    behavior = _normalize_ai_agent_behavior(settings, actor_role=actor_role)

    if public["operation_mode"] == "readonly" and _contains_audit_mode_prohibited_action(_extract_text_from_messages(normalized_messages)):
        raise AiAgentError("AI Agent 目前為唯讀模式，僅提供查詢與排查建議，不接受操作類指令。", http_status=403)

    if public["operation_mode"] == "audit" and actor_role != "super_admin":
        raise AiAgentError("AI Agent 目前為審計模式，僅 root 可執行。", http_status=403)

    if public["operation_mode"] == "write" and actor_role != "super_admin":
        raise AiAgentError("AI Agent 目前為執行寫入模式，僅 root 可執行。", http_status=403)

    system_prompt = _ai_agent_system_prompt(
        behavior,
        role=actor_role,
        actor=actor,
        allow_tool_runs=bool(public["allow_tool_runs"]),
        operation_mode=public["operation_mode"],
    )
    max_prompt_chars = public["max_prompt_chars"]
    sanitized_messages = _trim_chat_history_to_budget(sanitized_messages, max_prompt_chars)
    if _message_text_length(sanitized_messages) > max_prompt_chars:
        raise AiAgentError(f"訊息內容超過上限 {max_prompt_chars} 字", http_status=413)
    sanitized_messages = [{"role": "system", "content": system_prompt}, *sanitized_messages]
    requested_model = str(model or "").strip()
    model_name = (
        normalize_ai_agent_model(requested_model)
        if requested_model
        else (public["model"] or DEFAULT_AI_AGENT_MODEL)
    )
    if not model_name:
        raise AiAgentError("model 格式不正確", http_status=400)
    allowed_models = [item for item in str(public.get("allowed_models") or "").split(",") if item]
    if allowed_models and model_name not in allowed_models:
        raise AiAgentError("model 不在允許清單，請改用允許的模型", http_status=400)
    payload = {
        "model": model_name,
        "messages": sanitized_messages,
        "stream": False,
    }
    response = _json_request(settings, "POST", "/chat/completions", payload, session_key=session_key)
    if _contains_mock_phrase(response):
        raise AiAgentError("AI Agent 後端仍回傳 mock 回覆，請確認 ai_agent_api_base_url 是否指向真實 Hermes endpoint")
    if isinstance(response, dict):
        hermes_meta = response.get("hermes") if isinstance(response.get("hermes"), dict) else {}
        hermes_error = str(hermes_meta.get("error") or "").strip()
        if hermes_meta.get("failed") is True or hermes_meta.get("completed") is False:
            raise AiAgentError(
                f"AI Agent 後端執行失敗：{hermes_error or 'Hermes 回報 failed'}",
                payload=response,
            )
    choices = response.get("choices") if isinstance(response, dict) else None
    message = {}
    finish_reason = ""
    if choices and isinstance(choices, list) and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        finish_reason = str(choices[0].get("finish_reason") or "")
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        content = "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict))
    if finish_reason == "error" or str(content or "").lstrip().lower().startswith("api call failed after"):
        raise AiAgentError(
            f"AI Agent 後端執行失敗：{str(content or '').strip() or 'chat completion failed'}",
            payload=response if isinstance(response, dict) else None,
        )
    normalized = str(content or "").strip().lower()
    if _is_mock_chat_reply(normalized):
        raise AiAgentError("AI Agent 後端仍回傳 mock 回覆，請確認 ai_agent_api_base_url 是否指向真實 Hermes endpoint")
    return {
        "content": str(content or ""),
        "model": response.get("model") if isinstance(response, dict) else model_name,
        "usage": response.get("usage") if isinstance(response, dict) else None,
        "raw": response,
    }


def _is_mock_chat_reply(content):
    compact = _compact_mock_text(content)
    if not compact:
        return False
    if compact in KNOWN_MOCK_CHAT_REPLIES:
        return True
    if "mockhermesresponse" in compact and _has_mock_request_phrase(compact):
        return True
    return False
