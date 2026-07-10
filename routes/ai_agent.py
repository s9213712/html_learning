import json
from datetime import datetime
import hashlib
import io
import os
import re
import shutil
import threading
from urllib.parse import urlencode

from flask import request

from services.ai_agent.hermes import (
    AiAgentError,
    AI_AGENT_TOOL_ARGUMENT_HINTS,
    AI_AGENT_TOOL_BLUEPRINT,
    ai_agent_capabilities,
    ai_agent_chat,
    ai_agent_health,
    public_ai_agent_audit_status,
    run_ai_agent_audit_scan,
    ai_agent_write_guard_status,
    ai_agent_models,
    filter_retired_ai_agent_models,
    _is_mock_chat_reply,
    public_ai_agent_settings,
)
from services.comfyui.template.analyzer import FieldCategory, analyze_workflow_json
from services.storage.catalog import create_share_link


AI_AGENT_CHAT_WORKER_LIMIT = max(1, int(os.environ.get("HACKME_AI_AGENT_CHAT_WORKER_LIMIT", "8") or "8"))
AI_AGENT_CHAT_WORKERS = threading.BoundedSemaphore(AI_AGENT_CHAT_WORKER_LIMIT)
AI_AGENT_COMFYUI_SHORTCUT_WORKFLOWS = {
    "inpaint": "origin_sdxl_checkpoint_inpaint",
    "outpaint": "origin_flux_fill_outpaint_gguf_q3",
}
AI_AGENT_COMFYUI_LEGACY_SHORTCUT_WORKFLOWS = {"", "origin_sdxl_txt2img"}


AI_AGENT_WRITE_TOOL_SPECS = {
    "write_community_create_thread": {
        "label": "發表主題",
        "description": "在指定討論版建立主題。",
        "method": "POST",
        "path": "/api/community/boards/{board_id}/threads",
        "path_params": {"board_id": "positive_int"},
        "body_fields": {"title", "content", "post_type"},
        "required": {"board_id", "title", "content"},
        "write": True,
    },
    "write_community_reply_thread": {
        "label": "回覆主題",
        "description": "在指定主題留言。",
        "method": "POST",
        "path": "/api/community/threads/{thread_id}/posts",
        "path_params": {"thread_id": "positive_int"},
        "body_fields": {"content"},
        "required": {"thread_id", "content"},
        "write": True,
    },
    "write_comfyui_generate": {
        "label": "執行生圖",
        "description": "送出 ComfyUI 生圖任務，參數仍由 ComfyUI API 驗證。",
        "method": "POST",
        "path": "/api/comfyui/generate",
        "path_params": {},
        "body_fields": {
            "prompt", "edit_instruction", "edit_prompt", "negative_prompt", "model", "checkpoint", "checkpoint_name", "width", "height",
            "steps", "cfg", "cfg_scale", "sampler", "sampler_name", "scheduler", "seed", "batch_size",
            "generation_mode", "source_image_ref", "source_image_ref_json", "mask_image_ref",
            "mask_image_ref_json", "reference_image_ref", "reference_image_ref_json",
            "pose_reference_image_ref", "pose_reference_ref", "control_image_ref", "control_image_ref_json",
            "controlnet", "controlnet_enabled", "controlnet_type", "controlnet_model",
            "controlnet_preprocessor", "control_strength", "control_start", "control_end",
            "denoise_strength", "outpaint_left", "outpaint_top",
            "outpaint_right", "outpaint_bottom", "outpaint_feathering",
            "workflow", "workflow_id", "official_workflow_id", "template_id", "lora",
            "loras", "vae", "vae_name", "timeout_seconds", "confirm_billing",
            "backend_url", "comfyui_backend_url", "qwen_edit_profile", "qwen_controlnet_profile", "qwen_profile",
            "profile", "qwen_reference_mode", "qwen_reference_image2", "qwen_reference_force_image2",
        },
        "required": {"prompt"},
        "write": True,
    },
    "write_comfyui_background_composite": {
        "label": "精確背景合成",
        "description": "使用站內 ComfyUI 圖片引用，把來源人物保留並以參考圖作為 exact background plate 合成；適合使用者明確要求完全複製背景時使用，不走模型重畫。",
        "method": "POST",
        "path": "/api/comfyui/background-composite",
        "path_params": {},
        "body_fields": {
            "source_image_ref", "source_image_ref_json", "background_image_ref",
            "background_image_ref_json", "reference_image_ref", "reference_image_ref_json",
            "mask_image_ref", "mask_image_ref_json", "width", "height",
            "background_fit", "mask_mode", "prompt", "confirm_billing",
        },
        "required": {"source_image_ref", "background_image_ref"},
        "write": True,
    },
    "write_chess_create_practice": {
        "label": "建立西洋棋練習",
        "description": "建立電腦對局練習。",
        "method": "POST",
        "path": "/api/games/chess/practice",
        "path_params": {},
        "body_fields": {"side", "human_side", "difficulty", "computer_difficulty"},
        "required": set(),
        "write": True,
    },
    "write_chess_make_move": {
        "label": "西洋棋走子",
        "description": "在指定棋局送出一步棋。",
        "method": "POST",
        "path": "/api/games/chess/matches/{match_id}/move",
        "path_params": {"match_id": "positive_int"},
        "body_fields": {"from", "to", "promotion"},
        "required": {"match_id", "from", "to"},
        "write": True,
    },
    "write_member_create_user": {
        "label": "新增會員",
        "description": "新增一般會員或管理者帳號；仍套用既有會員 API 限制。",
        "method": "POST",
        "path": "/api/admin/users",
        "path_params": {},
        "body_fields": {
            "username", "password", "password_confirm", "nickname", "real_name",
            "id_number", "birthdate", "phone", "role", "status", "member_level",
        },
        "required": {"username", "password", "password_confirm", "nickname"},
        "write": True,
    },
    "write_member_update_user": {
        "label": "更新會員",
        "description": "更新指定會員資料；此工具不提供刪除帳號。",
        "method": "PUT",
        "path": "/api/admin/users/{user_id}",
        "path_params": {"user_id": "positive_int"},
        "body_fields": {
            "nickname", "real_name", "id_number", "birthdate", "phone", "role",
            "status", "member_level", "base_level", "level_update_reason",
            "sanction_status", "sanction_until",
        },
        "required": {"user_id"},
        "write": True,
    },
    "write_member_set_avatar_from_cloud": {
        "label": "設定會員頭像",
        "description": "從該會員自己的站內雲端圖片設定頭像；可帶入 AI 判斷的裁切、旋轉與縮放後 crop。",
        "method": "DIRECT",
        "path_params": {},
        "body_fields": {
            "user_id", "cloud_file_id", "existing_file_id", "crop", "crop_json",
            "x", "y", "width", "height", "rotation", "zoom", "decision_reason",
            "confidence", "subject_detected", "crop_quality", "issues", "target_description",
            "preflight_ok", "preflight_crop_quality", "preflight_issues",
            "final_avatar_ok", "final_crop_quality", "final_issues",
        },
        "required": {"user_id", "cloud_file_id"},
        "write": True,
    },
    "write_bug_report_review": {
        "label": "審核 Bug 回報",
        "description": "審核 bug report，核准時可設定獎勵點數。",
        "method": "POST",
        "path": "/api/admin/bug-reports/{report_id}/review",
        "path_params": {"report_id": "safe_id"},
        "body_fields": {"decision", "review_note", "reward_points"},
        "required": {"report_id", "decision"},
        "write": True,
    },
    "write_launch_requirements_check": {
        "label": "上線需求檢查",
        "description": "讀取上線前 requirements gate 結果。",
        "method": "GET",
        "path": "/api/root/server-mode/requirements",
        "path_params": {},
        "query_fields": set(),
        "required": set(),
        "write": False,
    },
    "write_launch_preflight_execute": {
        "label": "執行上線前檢查與切換",
        "description": "執行上線前 requirements、log chain、AI audit scan，整理阻塞原因；gate 通過時可切換 production。",
        "method": "DIRECT",
        "path_params": {},
        "body_fields": {"target_mode", "mode", "auto_switch", "confirm", "reason", "force_audit"},
        "required": set(),
        "write": True,
    },
    "write_launch_logs_verify": {
        "label": "上線 log 鏈驗證",
        "description": "驗證 server-mode log chain。",
        "method": "GET",
        "path": "/api/root/server-mode/logs/verify",
        "path_params": {},
        "query_fields": set(),
        "required": set(),
        "write": False,
    },
    "write_launch_doc_read": {
        "label": "上線文件讀取",
        "description": "讀取 docs/ 內的 Markdown 上線文件。",
        "method": "GET",
        "path": "/api/root/launch-check/doc",
        "path_params": {},
        "query_fields": {"path"},
        "required": {"path"},
        "write": False,
    },
    "audit_scan": {
        "label": "立即審計掃描",
        "description": "觸發 AI Agent 審計掃描。",
        "method": "DIRECT",
        "path_params": {},
        "body_fields": {"force"},
        "required": set(),
        "write": False,
    },
    "write_codex_handoff_create": {
        "label": "建立 Codex 交接任務",
        "description": "建立給 Codex/root 審核接手的站內交接任務；只排程與紀錄，不直接執行 shell 或修改伺服器檔案。",
        "method": "DIRECT",
        "path_params": {},
        "body_fields": {
            "title", "objective", "context", "allowed_scope", "priority",
            "requested_artifacts", "safety_notes", "source_conversation_id",
        },
        "required": {"objective"},
        "write": True,
    },
    "write_trading_place_order": {
        "label": "建立交易掛單",
        "description": "透過既有交易 API 建立現貨/合約交易訂單。",
        "method": "POST",
        "path": "/api/trading/orders",
        "path_params": {},
        "body_fields": {"market_symbol", "side", "order_type", "quantity", "limit_price_points", "stop_loss_percent", "take_profit_percent", "emergency_close", "source_wallet_address"},
        "required": {"market_symbol", "side", "order_type", "quantity"},
        "write": True,
    },
    "write_trading_cancel_order": {
        "label": "取消交易掛單",
        "description": "取消指定交易訂單。",
        "method": "POST",
        "path": "/api/trading/orders/{order_uuid}/cancel",
        "path_params": {"order_uuid": "safe_id"},
        "body_fields": set(),
        "required": {"order_uuid"},
        "write": True,
    },
    "write_trading_bot_create": {
        "label": "建立交易機器人",
        "description": "建立 DCA/交易機器人；參數仍由交易 API 驗證。",
        "method": "POST",
        "path": "/api/trading/bots",
        "path_params": {},
        "body_fields": {"name", "bot_type", "market_symbol", "strategy", "enabled", "budget_points", "order_size_points", "interval_minutes", "max_runs", "parameters", "config"},
        "required": {"market_symbol"},
        "write": True,
    },
    "write_trading_bot_backtest": {
        "label": "交易機器人回測",
        "description": "執行交易策略回測。",
        "method": "POST",
        "path": "/api/trading/bots/backtest",
        "path_params": {},
        "body_fields": {"market_symbol", "strategy", "initial_cash", "lookback_days", "parameters", "config", "candles", "start_at", "end_at"},
        "required": {"market_symbol"},
        "write": True,
    },
    "write_trading_bot_scan": {
        "label": "執行交易機器人掃描",
        "description": "手動觸發目前使用者交易機器人掃描。",
        "method": "POST",
        "path": "/api/trading/bots/scan",
        "path_params": {},
        "body_fields": {"limit"},
        "required": set(),
        "write": True,
    },
    "write_trading_grid_preview": {
        "label": "網格交易預覽",
        "description": "預覽網格交易參數與費用。",
        "method": "POST",
        "path": "/api/trading/grid/preview",
        "path_params": {},
        "body_fields": {"market_symbol", "lower_price_points", "upper_price_points", "grid_count", "budget_points", "quantity", "config"},
        "required": {"market_symbol"},
        "write": True,
    },
    "write_trading_grid_bot_create": {
        "label": "建立網格交易機器人",
        "description": "建立網格交易機器人並由交易 API 驗證參數。",
        "method": "POST",
        "path": "/api/trading/grid-bots",
        "path_params": {},
        "body_fields": {"market_symbol", "lower_price_points", "upper_price_points", "grid_count", "budget_points", "enabled", "share_parameters", "config"},
        "required": {"market_symbol"},
        "write": True,
    },
    "write_trading_grid_bot_toggle": {
        "label": "切換網格機器人",
        "description": "啟用或停用指定網格機器人。",
        "method": "POST",
        "path": "/api/trading/grid-bots/{bot_uuid}/toggle",
        "path_params": {"bot_uuid": "safe_id"},
        "body_fields": {"enabled"},
        "required": {"bot_uuid", "enabled"},
        "write": True,
    },
    "write_trading_margin_open": {
        "label": "開立槓桿倉位",
        "description": "開立槓桿交易倉位；必須提供 idempotency_key 防重複。",
        "method": "POST",
        "path": "/api/trading/margin/open",
        "path_params": {},
        "body_fields": {"market_symbol", "position_type", "quantity", "collateral_points", "stop_loss_percent", "take_profit_percent", "idempotency_key"},
        "required": {"market_symbol", "position_type", "quantity", "collateral_points", "idempotency_key"},
        "write": True,
    },
    "write_trading_margin_close": {
        "label": "關閉槓桿倉位",
        "description": "關閉指定槓桿倉位。",
        "method": "POST",
        "path": "/api/trading/margin/{position_uuid}/close",
        "path_params": {"position_uuid": "safe_id"},
        "body_fields": set(),
        "required": {"position_uuid"},
        "write": True,
    },
    "write_trading_margin_add_collateral": {
        "label": "追加槓桿保證金",
        "description": "對指定槓桿倉位追加保證金。",
        "method": "POST",
        "path": "/api/trading/margin/{position_uuid}/collateral",
        "path_params": {"position_uuid": "safe_id"},
        "body_fields": {"amount_points", "idempotency_key"},
        "required": {"position_uuid", "amount_points", "idempotency_key"},
        "write": True,
    },
    "write_trading_margin_withdraw_collateral": {
        "label": "提領槓桿保證金",
        "description": "從指定槓桿倉位提領保證金。",
        "method": "POST",
        "path": "/api/trading/margin/{position_uuid}/collateral/withdraw",
        "path_params": {"position_uuid": "safe_id"},
        "body_fields": {"amount_points", "idempotency_key"},
        "required": {"position_uuid", "amount_points", "idempotency_key"},
        "write": True,
    },
    "write_trading_background_run_once": {
        "label": "執行交易背景任務",
        "description": "root 觸發交易背景 engine 單次執行。",
        "method": "POST",
        "path": "/api/root/trading/background/run-once",
        "path_params": {},
        "body_fields": {"limit", "reason"},
        "required": set(),
        "write": True,
    },
    "write_trading_liquidation_scan": {
        "label": "掃描借貸清算",
        "description": "root 觸發槓桿/借貸清算掃描。",
        "method": "POST",
        "path": "/api/root/trading/liquidations/scan",
        "path_params": {},
        "body_fields": {"limit", "market_symbol", "reason"},
        "required": set(),
        "write": True,
    },
    "write_trading_order_match": {
        "label": "撮合交易訂單",
        "description": "root 觸發交易訂單撮合。",
        "method": "POST",
        "path": "/api/root/trading/orders/match",
        "path_params": {},
        "body_fields": {"market_symbol", "limit"},
        "required": set(),
        "write": True,
    },
    "write_trading_bot_audit_run": {
        "label": "交易機器人審計",
        "description": "root 執行交易機器人審計。",
        "method": "POST",
        "path": "/api/root/trading/bot-audit/run",
        "path_params": {},
        "body_fields": {"limit", "reason"},
        "required": set(),
        "write": True,
    },
    "write_trading_verify_jobs": {
        "label": "交易記帳驗證",
        "description": "root 觸發交易/鏈上記帳驗證 job。",
        "method": "POST",
        "path": "/api/root/trading/verify/jobs",
        "path_params": {},
        "body_fields": {"scope", "limit", "reason"},
        "required": set(),
        "write": True,
    },
    "write_trading_market_update": {
        "label": "更新交易市場",
        "description": "root 更新指定交易市場設定。",
        "method": "POST",
        "path": "/api/root/trading/markets/{symbol}",
        "path_params": {"symbol": "safe_path"},
        "body_fields": {"enabled", "manual_price_points", "price_source", "fee_rate_percent", "min_order_points", "max_order_points"},
        "required": {"symbol"},
        "write": True,
    },
    "write_cloud_drive_create_text": {
        "label": "建立雲端文字檔",
        "description": "在雲端硬碟建立文字檔。",
        "method": "POST",
        "path": "/api/cloud-drive/files/text",
        "path_params": {},
        "body_fields": {"filename", "content", "privacy_mode", "virtual_path"},
        "required": {"filename", "content"},
        "write": True,
    },
    "write_cloud_drive_upload": {
        "label": "建立雲端文字檔",
        "description": "AI Agent JSON 版上傳：建立文字檔；二進位上傳需使用使用者選檔流程。",
        "method": "POST",
        "path": "/api/cloud-drive/files/text",
        "path_params": {},
        "body_fields": {"filename", "content", "privacy_mode", "virtual_path"},
        "required": {"filename", "content"},
        "write": True,
    },
    "write_cloud_drive_delete": {
        "label": "刪除雲端檔案",
        "description": "刪除指定雲端硬碟檔案。",
        "method": "DELETE",
        "path": "/api/cloud-drive/files/{file_id}",
        "path_params": {"file_id": "safe_id"},
        "body_fields": set(),
        "required": {"file_id"},
        "write": True,
    },
    "write_cloud_drive_remote_download": {
        "label": "建立遠端下載",
        "description": "建立 Direct 或 BT 遠端下載任務。",
        "method": "POST",
        "path": "/api/cloud-drive/remote-download/tasks",
        "path_params": {},
        "body_fields": {"url", "source_type", "download_mode", "privacy_mode", "virtual_path", "filename"},
        "required": {"url"},
        "write": True,
    },
    "write_remote_download_direct": {
        "label": "建立 Direct download",
        "description": "建立 Direct download 任務。",
        "method": "POST",
        "path": "/api/cloud-drive/remote-download/tasks",
        "path_params": {},
        "body_fields": {"url", "download_mode", "privacy_mode", "virtual_path", "filename"},
        "required": {"url"},
        "write": True,
    },
    "write_remote_download_bt": {
        "label": "建立 BT/magnet download",
        "description": "建立 magnet 或 .torrent URL 下載任務。",
        "method": "POST",
        "path": "/api/cloud-drive/remote-download/tasks",
        "path_params": {},
        "body_fields": {"url", "download_mode", "privacy_mode", "virtual_path", "filename"},
        "required": {"url"},
        "write": True,
    },
    "write_remote_download_pause": {
        "label": "暫停遠端下載",
        "description": "暫停指定遠端下載任務。",
        "method": "POST",
        "path": "/api/cloud-drive/remote-download/tasks/{task_id}/pause",
        "path_params": {"task_id": "safe_id"},
        "body_fields": set(),
        "required": {"task_id"},
        "write": True,
    },
    "write_remote_download_resume": {
        "label": "恢復遠端下載",
        "description": "恢復指定遠端下載任務。",
        "method": "POST",
        "path": "/api/cloud-drive/remote-download/tasks/{task_id}/resume",
        "path_params": {"task_id": "safe_id"},
        "body_fields": set(),
        "required": {"task_id"},
        "write": True,
    },
    "write_remote_download_cancel": {
        "label": "取消遠端下載",
        "description": "取消指定遠端下載任務。",
        "method": "POST",
        "path": "/api/cloud-drive/remote-download/tasks/{task_id}/cancel",
        "path_params": {"task_id": "safe_id"},
        "body_fields": set(),
        "required": {"task_id"},
        "write": True,
    },
    "write_remote_download_recover": {
        "label": "恢復中斷下載",
        "description": "恢復指定中斷遠端下載任務。",
        "method": "POST",
        "path": "/api/cloud-drive/remote-download/tasks/{task_id}/recover",
        "path_params": {"task_id": "safe_id"},
        "body_fields": set(),
        "required": {"task_id"},
        "write": True,
    },
    "write_share_create": {
        "label": "建立檔案分享",
        "description": "為雲端硬碟檔案建立分享連結。",
        "method": "DIRECT",
        "path_params": {},
        "body_fields": {"storage_file_id", "file_id", "expires_at", "can_preview", "access_scope", "required_user_id", "required_username", "max_views", "wrapped_file_key_envelope", "share_password"},
        "required": set(),
        "write": True,
    },
    "write_share_update": {
        "label": "更新分享",
        "description": "更新檔案、相簿或影音分享設定。",
        "method": "PUT",
        "path": "/api/shares/{share_type}/{share_id}",
        "path_params": {"share_type": "safe_id", "share_id": "safe_id"},
        "body_fields": {"expires_at", "max_views", "access_scope", "required_user_id", "required_username", "share_password", "clear_share_password", "reset_access_count"},
        "required": {"share_type", "share_id"},
        "write": True,
    },
    "write_share_revoke": {
        "label": "撤銷分享",
        "description": "撤銷檔案、相簿或影音分享。",
        "method": "POST",
        "path": "/api/shares/{share_type}/{share_id}/revoke",
        "path_params": {"share_type": "safe_id", "share_id": "safe_id"},
        "body_fields": set(),
        "required": {"share_type", "share_id"},
        "write": True,
    },
    "write_task_cancel": {
        "label": "取消任務",
        "description": "取消 Job Center 任務。",
        "method": "POST",
        "path": "/api/jobs/{job_uuid}/cancel",
        "path_params": {"job_uuid": "safe_id"},
        "body_fields": set(),
        "required": {"job_uuid"},
        "write": True,
    },
    "write_task_retry": {
        "label": "重試任務",
        "description": "重試 Job Center 任務。",
        "method": "POST",
        "path": "/api/jobs/{job_uuid}/retry",
        "path_params": {"job_uuid": "safe_id"},
        "body_fields": set(),
        "required": {"job_uuid"},
        "write": True,
    },
    "write_automation_job_run": {
        "label": "重試自動化任務",
        "description": "以 Job Center retry handler 執行可重試的自動化任務。",
        "method": "POST",
        "path": "/api/jobs/{job_uuid}/retry",
        "path_params": {"job_uuid": "safe_id"},
        "body_fields": set(),
        "required": {"job_uuid"},
        "write": True,
    },
    "write_album_create": {
        "label": "建立相簿",
        "description": "建立雲端相簿。",
        "method": "POST",
        "path": "/api/storage/albums",
        "path_params": {},
        "body_fields": {"title", "description", "visibility", "share_password"},
        "required": {"title"},
        "write": True,
    },
    "write_album_update": {
        "label": "更新相簿",
        "description": "更新雲端相簿設定。",
        "method": "PUT",
        "path": "/api/storage/albums/{album_id}",
        "path_params": {"album_id": "safe_id"},
        "body_fields": {"title", "description", "visibility", "share_password", "clear_share_password"},
        "required": {"album_id"},
        "write": True,
    },
    "write_album_delete": {
        "label": "刪除相簿",
        "description": "刪除雲端相簿。",
        "method": "DELETE",
        "path": "/api/storage/albums/{album_id}",
        "path_params": {"album_id": "safe_id"},
        "body_fields": set(),
        "required": {"album_id"},
        "write": True,
    },
    "write_album_add_file": {
        "label": "加入相簿檔案",
        "description": "把雲端檔案加入指定相簿。",
        "method": "POST",
        "path": "/api/storage/albums/{album_id}/files",
        "path_params": {"album_id": "safe_id"},
        "body_fields": {"storage_file_id", "file_id", "caption", "sort_order"},
        "required": {"album_id"},
        "write": True,
    },
    "write_album_remove_file": {
        "label": "移除相簿檔案",
        "description": "從指定相簿移除檔案。",
        "method": "DELETE",
        "path": "/api/storage/albums/{album_id}/files/{album_file_id}",
        "path_params": {"album_id": "safe_id", "album_file_id": "safe_id"},
        "body_fields": set(),
        "required": {"album_id", "album_file_id"},
        "write": True,
    },
    "write_album_smart_organize": {
        "label": "相簿智慧整理",
        "description": "依站內策略自動整理相簿。",
        "method": "POST",
        "path": "/api/storage/albums/smart-organize",
        "path_params": {},
        "body_fields": {"strategy", "visibility"},
        "required": set(),
        "write": True,
    },
    "write_video_upload": {
        "label": "AI Agent JSON 版影音發布",
        "description": "AI Agent JSON 版影音發布：使用既有 cloud_file_id 發布影音並可排程 HLS。",
        "method": "POST",
        "path": "/api/videos/publish",
        "path_params": {},
        "body_fields": {"cloud_file_id", "title", "description", "visibility", "cover_file_id", "share_password", "share_expires_at", "share_max_views", "streaming_modes"},
        "required": {"cloud_file_id", "title"},
        "write": True,
    },
    "write_video_publish": {
        "label": "發布既有雲端影音",
        "description": "使用既有 cloud_file_id 發布影音並可排程 HLS。",
        "method": "POST",
        "path": "/api/videos/publish",
        "path_params": {},
        "body_fields": {"cloud_file_id", "title", "description", "visibility", "cover_file_id", "share_password", "share_expires_at", "share_max_views", "streaming_modes"},
        "required": {"cloud_file_id", "title"},
        "write": True,
    },
    "write_video_update": {
        "label": "更新影音",
        "description": "更新影音標題、描述、可見性等設定。",
        "method": "PUT",
        "path": "/api/videos/{video_id}/manage",
        "path_params": {"video_id": "positive_int"},
        "body_fields": {"title", "description", "visibility", "share_password", "share_expires_at", "share_max_views", "streaming_modes"},
        "required": {"video_id"},
        "write": True,
    },
    "write_video_delete": {
        "label": "刪除影音",
        "description": "刪除指定影音。",
        "method": "DELETE",
        "path": "/api/videos/{video_id}/manage",
        "path_params": {"video_id": "positive_int"},
        "body_fields": set(),
        "required": {"video_id"},
        "write": True,
    },
    "write_video_streaming_modes": {
        "label": "更新影音串流模式",
        "description": "更新指定影音的串流模式。",
        "method": "PUT",
        "path": "/api/videos/{video_id}/streaming-modes",
        "path_params": {"video_id": "positive_int"},
        "body_fields": {"streaming_modes"},
        "required": {"video_id", "streaming_modes"},
        "write": True,
    },
    "write_transcode_hls": {
        "label": "排程 HLS 轉檔",
        "description": "對指定雲端影音檔案排程 HLS 轉檔。",
        "method": "POST",
        "path": "/api/media/{file_id}/prepare-stream",
        "path_params": {"file_id": "safe_id"},
        "body_fields": set(),
        "required": {"file_id"},
        "write": True,
    },
    "write_hls_rebuild": {
        "label": "重建 HLS",
        "description": "強制重新排程指定影音檔案 HLS 轉檔。",
        "method": "POST",
        "path": "/api/media/{file_id}/prepare-stream",
        "path_params": {"file_id": "safe_id"},
        "body_fields": set(),
        "required": {"file_id"},
        "write": True,
    },
    "write_subtitle_upload": {
        "label": "上傳字幕文字",
        "description": "把字幕文字作為站內字幕檔加入影音。",
        "method": "DIRECT",
        "path_params": {},
        "body_fields": {"video_id", "subtitle_text", "filename", "label", "language"},
        "required": {"video_id", "subtitle_text"},
        "write": True,
    },
    "write_community_thread_reward": {
        "label": "獎勵主題作者",
        "description": "對討論區主題作者加聲望獎勵。",
        "method": "POST",
        "path": "/api/community/threads/{thread_id}/reward",
        "path_params": {"thread_id": "positive_int"},
        "body_fields": {"points", "reason"},
        "required": {"thread_id", "points"},
        "write": True,
    },
    "write_community_post_penalty": {
        "label": "處罰留言作者",
        "description": "對討論區留言作者加違規點數。",
        "method": "POST",
        "path": "/api/community/posts/{post_id}/penalty",
        "path_params": {"post_id": "positive_int"},
        "body_fields": {"points", "reason"},
        "required": {"post_id", "points"},
        "write": True,
    },
    "write_points_governance_execute": {
        "label": "執行治理提案",
        "description": "執行指定點數鏈治理提案。",
        "method": "POST",
        "path": "/api/root/points/governance/proposals/{proposal_uuid}/execute",
        "path_params": {"proposal_uuid": "safe_path"},
        "body_fields": set(),
        "required": {"proposal_uuid"},
        "write": True,
    },
    "write_points_governance_sponsor": {
        "label": "贊助治理提案",
        "description": "贊助指定點數鏈治理提案。",
        "method": "POST",
        "path": "/api/admin/points/governance/proposals/{proposal_uuid}/sponsor",
        "path_params": {"proposal_uuid": "safe_path"},
        "body_fields": set(),
        "required": {"proposal_uuid"},
        "write": True,
    },
    "write_points_governance_cancel": {
        "label": "取消治理提案",
        "description": "取消指定點數鏈治理提案。",
        "method": "POST",
        "path": "/api/admin/points/governance/proposals/{proposal_uuid}/cancel",
        "path_params": {"proposal_uuid": "safe_path"},
        "body_fields": {"reason"},
        "required": {"proposal_uuid", "reason"},
        "write": True,
    },
    "write_points_wallet_freeze_proposal": {
        "label": "建立錢包凍結治理提案",
        "description": "建立錢包凍結或解除凍結治理提案。",
        "method": "POST",
        "path": "/api/root/points/governance/wallet-freeze",
        "path_params": {},
        "body_fields": {"wallet_address", "address", "reason", "evidence", "reference", "action"},
        "required": {"reason"},
        "write": True,
    },
    "write_points_wallet_transfer": {
        "label": "提交錢包轉帳",
        "description": "提交站內點數鏈錢包轉帳交易；仍套用既有錢包、簽章、防重複與限制檢查。",
        "method": "POST",
        "path": "/api/points/transactions/submit",
        "path_params": {},
        "body_fields": {
            "source_wallet_address", "destination_wallet_address", "from", "to",
            "amount_points", "value", "fee_points", "request_uuid", "memo",
            "signature", "wallet_signature", "compact",
        },
        "required": {"source_wallet_address", "destination_wallet_address", "amount_points", "request_uuid"},
        "write": True,
    },
    "write_server_integrity_repair": {
        "label": "修復完整性鏈",
        "description": "root 執行 audit/violation chain reseal 修復。",
        "method": "POST",
        "path": "/api/admin/integrity/repair",
        "path_params": {},
        "body_fields": set(),
        "required": set(),
        "write": True,
    },
    "write_server_restart": {
        "label": "重啟伺服器",
        "description": "root 排程站內伺服器重啟。",
        "method": "POST",
        "path": "/api/admin/restart",
        "path_params": {},
        "body_fields": {"reason"},
        "required": set(),
        "write": True,
    },
    "write_server_mode_checkpoint": {
        "label": "建立伺服器模式 checkpoint",
        "description": "建立 server-mode checkpoint。",
        "method": "POST",
        "path": "/api/root/server-mode/checkpoint",
        "path_params": {},
        "body_fields": {"target_mode", "mode", "reason", "notes"},
        "required": set(),
        "write": True,
    },
    "write_server_mode_switch": {
        "label": "切換伺服器模式",
        "description": "切換 server-mode profile。",
        "method": "POST",
        "path": "/api/root/server-mode/switch",
        "path_params": {},
        "body_fields": {"mode", "target_mode", "confirm", "reason", "notes"},
        "required": {"mode", "confirm"},
        "write": True,
    },
    "write_incident_enter": {
        "label": "進入緊急事件模式",
        "description": "root 進入 incident lockdown。",
        "method": "POST",
        "path": "/api/root/incident/enter",
        "path_params": {},
        "body_fields": {"confirm", "trigger_type", "reason", "verification"},
        "required": {"confirm", "reason"},
        "write": True,
    },
    "write_incident_resolve": {
        "label": "解除緊急事件模式",
        "description": "root 解除 incident lockdown。",
        "method": "POST",
        "path": "/api/root/incident/resolve",
        "path_params": {},
        "body_fields": {"confirm", "notes", "verification"},
        "required": {"confirm"},
        "write": True,
    },
}


AI_AGENT_WRITE_TOOL_SPECS.update({
    "write_chat_create_room": {
        "label": "建立聊天室",
        "description": "建立站內聊天室，可設定隱私、匿名與初始成員。",
        "method": "POST",
        "path": "/api/chat/rooms",
        "path_params": {},
        "body_fields": {"name", "target_user", "allow_anonymous", "anonymous", "anonymous_enabled", "join_password", "invite_usernames"},
        "required": set(),
        "write": True,
    },
    "write_chat_join_room": {
        "label": "加入聊天室",
        "description": "加入指定聊天室。",
        "method": "POST",
        "path": "/api/chat/rooms/{room_id}/join",
        "path_params": {"room_id": "positive_int"},
        "body_fields": {"password", "use_anonymous", "anonymous_name"},
        "required": {"room_id"},
        "write": True,
    },
    "write_chat_invite": {
        "label": "邀請聊天室成員",
        "description": "邀請站內會員加入指定聊天室。",
        "method": "POST",
        "path": "/api/chat/rooms/{room_id}/invites",
        "path_params": {"room_id": "positive_int"},
        "body_fields": {"username", "usernames", "user_id", "message"},
        "required": {"room_id"},
        "write": True,
    },
    "write_chat_send_message": {
        "label": "送出聊天室訊息",
        "description": "在指定聊天室送出訊息。",
        "method": "POST",
        "path": "/api/chat/rooms/{room_id}/messages",
        "path_params": {"room_id": "positive_int"},
        "body_fields": {"content", "attachments", "reply_to_message_id"},
        "required": {"room_id", "content"},
        "write": True,
    },
    "write_chat_edit_message": {
        "label": "編輯聊天室訊息",
        "description": "編輯站內聊天室訊息。",
        "method": "PUT",
        "path": "/api/chat/messages/{message_id}",
        "path_params": {"message_id": "positive_int"},
        "body_fields": {"content"},
        "required": {"message_id", "content"},
        "write": True,
    },
    "write_chat_delete_message": {
        "label": "刪除聊天室訊息",
        "description": "刪除站內聊天室訊息。",
        "method": "DELETE",
        "path": "/api/chat/messages/{message_id}",
        "path_params": {"message_id": "positive_int"},
        "body_fields": set(),
        "required": {"message_id"},
        "write": True,
    },
    "write_chat_friend_request": {
        "label": "送出好友邀請",
        "description": "向站內會員送出好友邀請。",
        "method": "POST",
        "path": "/api/chat/friends/requests",
        "path_params": {},
        "body_fields": {"username", "target_user_id", "message"},
        "required": {"username"},
        "write": True,
    },
    "write_chat_friend_decide": {
        "label": "處理好友邀請",
        "description": "接受或拒絕好友邀請。",
        "method": "POST",
        "path": "/api/chat/friends/requests/{request_id}/{decision}",
        "path_params": {"request_id": "positive_int", "decision": "safe_id"},
        "body_fields": {"reason"},
        "required": {"request_id", "decision"},
        "write": True,
    },
    "write_appeal_create": {
        "label": "建立申訴",
        "description": "提交站內申訴或覆核請求。",
        "method": "POST",
        "path": "/api/appeals",
        "path_params": {},
        "body_fields": {"appeal_type", "subject", "content", "target_type", "target_id", "evidence"},
        "required": {"subject", "content"},
        "write": True,
    },
    "write_appeal_review": {
        "label": "審核申訴",
        "description": "root/管理員審核申訴並記錄處置。",
        "method": "POST",
        "path": "/api/admin/appeals/{appeal_id}/review",
        "path_params": {"appeal_id": "positive_int"},
        "body_fields": {"action", "note", "reward_points"},
        "required": {"appeal_id", "action"},
        "write": True,
    },
    "write_notification_send": {
        "label": "發送站內通知",
        "description": "向站內使用者或群組發送通知。",
        "method": "POST",
        "path": "/api/admin/notifications/send",
        "path_params": {},
        "body_fields": {"user_id", "user_ids", "target_user_id", "title", "body", "type", "link"},
        "required": {"title", "body"},
        "write": True,
    },
    "write_report_claim": {
        "label": "認領檢舉案件",
        "description": "認領站內檢舉案件。",
        "method": "POST",
        "path": "/api/admin/reports/{report_id}/claim",
        "path_params": {"report_id": "positive_int"},
        "body_fields": set(),
        "required": {"report_id"},
        "write": True,
    },
    "write_report_resolve": {
        "label": "結案檢舉案件",
        "description": "結案站內檢舉案件並記錄結果。",
        "method": "POST",
        "path": "/api/admin/reports/{report_id}/resolve",
        "path_params": {"report_id": "positive_int"},
        "body_fields": {"resolution", "review_note", "action", "reward_points"},
        "required": {"report_id", "resolution"},
        "write": True,
    },
    "write_user_review_registration": {
        "label": "審核會員註冊",
        "description": "審核待審會員註冊。",
        "method": "POST",
        "path": "/api/admin/users/{user_id}/review-registration",
        "path_params": {"user_id": "positive_int"},
        "body_fields": {"decision", "review_note"},
        "required": {"user_id", "decision"},
        "write": True,
    },
    "write_user_block": {
        "label": "封鎖或解除會員",
        "description": "封鎖、停權或解除會員限制。",
        "method": "POST",
        "path": "/api/admin/users/{user_id}/block",
        "path_params": {"user_id": "positive_int"},
        "body_fields": {"blocked", "duration_minutes", "reason"},
        "required": {"user_id"},
        "write": True,
    },
    "write_user_add_violation": {
        "label": "新增會員違規",
        "description": "對會員新增違規點數或紀錄。",
        "method": "POST",
        "path": "/api/admin/users/{user_id}/violation",
        "path_params": {"user_id": "positive_int"},
        "body_fields": {"reason", "points", "severity"},
        "required": {"user_id", "reason"},
        "write": True,
    },
    "write_user_reset_violations": {
        "label": "重置會員違規",
        "description": "重置指定會員違規紀錄。",
        "method": "POST",
        "path": "/api/admin/users/{user_id}/reset-violations",
        "path_params": {"user_id": "positive_int"},
        "body_fields": {"reason"},
        "required": {"user_id"},
        "write": True,
    },
    "write_moderation_note": {
        "label": "新增會員管理備註",
        "description": "新增站內會員管理備註。",
        "method": "POST",
        "path": "/api/admin/mod-notes/{user_id}",
        "path_params": {"user_id": "positive_int"},
        "body_fields": {"note", "severity", "visibility"},
        "required": {"user_id", "note"},
        "write": True,
    },
    "write_moderation_proposal_create": {
        "label": "建立治理處分提案",
        "description": "建立會員治理、處分或管理提案。",
        "method": "POST",
        "path": "/api/admin/moderation/proposals",
        "path_params": {},
        "body_fields": {"target_user_id", "action", "reason", "evidence", "duration_minutes", "points"},
        "required": {"target_user_id", "action", "reason"},
        "write": True,
    },
    "write_moderation_proposal_vote": {
        "label": "投票治理處分提案",
        "description": "對會員治理提案投票。",
        "method": "POST",
        "path": "/api/admin/moderation/proposals/{proposal_id}/vote",
        "path_params": {"proposal_id": "positive_int"},
        "body_fields": {"vote", "reason"},
        "required": {"proposal_id", "vote"},
        "write": True,
    },
    "write_moderation_proposal_execute": {
        "label": "執行治理處分提案",
        "description": "執行已通過的會員治理提案。",
        "method": "POST",
        "path": "/api/admin/moderation/proposals/{proposal_id}/execute",
        "path_params": {"proposal_id": "positive_int"},
        "body_fields": {"reason"},
        "required": {"proposal_id"},
        "write": True,
    },
    "write_moderation_proposal_override": {
        "label": "root 覆寫治理提案",
        "description": "root 覆寫會員治理提案決策。",
        "method": "POST",
        "path": "/api/root/moderation/proposals/{proposal_id}/override",
        "path_params": {"proposal_id": "positive_int"},
        "body_fields": {"decision", "reason"},
        "required": {"proposal_id", "decision", "reason"},
        "write": True,
    },
    "write_storage_quota_override": {
        "label": "設定雲端容量覆寫",
        "description": "root 設定指定會員雲端硬碟容量覆寫。",
        "method": "PUT",
        "path": "/api/root/storage/users/{user_id}/quota-override",
        "path_params": {"user_id": "positive_int"},
        "body_fields": {"quota_mb", "max_file_size_mb", "upload_rate_limit_per_day", "can_upload", "enabled", "reason"},
        "required": {"user_id", "reason"},
        "write": True,
    },
    "write_storage_quota_override_clear": {
        "label": "清除雲端容量覆寫",
        "description": "root 清除指定會員雲端硬碟容量覆寫。",
        "method": "DELETE",
        "path": "/api/root/storage/users/{user_id}/quota-override",
        "path_params": {"user_id": "positive_int"},
        "body_fields": {"reason"},
        "required": {"user_id"},
        "write": True,
    },
    "write_storage_sync_quota": {
        "label": "同步雲端容量",
        "description": "執行雲端硬碟容量同步或 dry-run。",
        "method": "POST",
        "path": "/api/admin/storage/sync-quota",
        "path_params": {},
        "body_fields": {"user_id", "dry_run", "reason"},
        "required": set(),
        "write": True,
    },
    "write_storage_trash_purge": {
        "label": "清理雲端垃圾桶",
        "description": "執行雲端硬碟垃圾桶清理。",
        "method": "POST",
        "path": "/api/admin/storage/trash/purge",
        "path_params": {},
        "body_fields": {"older_than_days", "confirm", "reason"},
        "required": set(),
        "write": True,
    },
    "write_storage_maintenance": {
        "label": "執行雲端維護",
        "description": "執行雲端硬碟維護動作。",
        "method": "POST",
        "path": "/api/admin/storage/maintenance",
        "path_params": {},
        "body_fields": {"action", "dry_run", "reason"},
        "required": {"action"},
        "write": True,
    },
    "write_cloud_drive_text_update": {
        "label": "更新雲端文字檔",
        "description": "更新雲端硬碟文字檔內容。",
        "method": "PUT",
        "path": "/api/cloud-drive/files/{file_id}/text",
        "path_params": {"file_id": "safe_id"},
        "body_fields": {"content"},
        "required": {"file_id", "content"},
        "write": True,
    },
    "write_comfyui_start": {
        "label": "啟動 ComfyUI 連線",
        "description": "啟動或切換 ComfyUI 站內連線模式。",
        "method": "POST",
        "path": "/api/comfyui/start",
        "path_params": {},
        "body_fields": {"backend_url", "mode", "reason"},
        "required": set(),
        "write": True,
    },
    "write_comfyui_stop": {
        "label": "停止 ComfyUI",
        "description": "root 停止站內 ComfyUI 後端流程。",
        "method": "POST",
        "path": "/api/root/comfyui/stop",
        "path_params": {},
        "body_fields": {"reason"},
        "required": set(),
        "write": True,
    },
    "write_comfyui_interrupt": {
        "label": "中斷 ComfyUI 任務",
        "description": "中斷目前 ComfyUI 執行中的任務。",
        "method": "POST",
        "path": "/api/comfyui/interrupt",
        "path_params": {},
        "body_fields": {"reason"},
        "required": set(),
        "write": True,
    },
    "write_comfyui_save_image": {
        "label": "保存 ComfyUI 圖片",
        "description": "把 ComfyUI 產物保存到站內雲端硬碟。",
        "method": "POST",
        "path": "/api/comfyui/save",
        "path_params": {},
        "body_fields": {"job_id", "history_id", "filename", "target_folder"},
        "required": set(),
        "write": True,
    },
    "write_comfyui_share_image": {
        "label": "分享 ComfyUI 圖片",
        "description": "建立 ComfyUI 產圖分享。",
        "method": "POST",
        "path": "/api/comfyui/share",
        "path_params": {},
        "body_fields": {"job_id", "history_id", "filename", "scope", "share_password", "expires_at"},
        "required": set(),
        "write": True,
    },
    "write_comfyui_favorite_image": {
        "label": "收藏 ComfyUI 圖片",
        "description": "將 ComfyUI 產圖加入收藏。",
        "method": "POST",
        "path": "/api/comfyui/image-favorites",
        "path_params": {},
        "body_fields": {"image_ref", "job_id", "history_id", "filename", "note", "tags"},
        "required": set(),
        "write": True,
    },
    "write_comfyui_delete_favorite": {
        "label": "刪除 ComfyUI 收藏",
        "description": "刪除 ComfyUI 圖片收藏。",
        "method": "DELETE",
        "path": "/api/comfyui/image-favorites/{favorite_id}",
        "path_params": {"favorite_id": "positive_int"},
        "body_fields": set(),
        "required": {"favorite_id"},
        "write": True,
    },
    "write_comfyui_workflow_run": {
        "label": "執行 ComfyUI workflow",
        "description": "執行已儲存的 ComfyUI workflow preset。",
        "method": "POST",
        "path": "/api/comfyui/workflows/{preset_id}/run",
        "path_params": {"preset_id": "positive_int"},
        "body_fields": {"inputs", "parameters", "confirm_billing"},
        "required": {"preset_id"},
        "write": True,
    },
    "write_comfyui_workflow_import": {
        "label": "匯入 ComfyUI workflow",
        "description": "匯入 ComfyUI workflow preset。",
        "method": "POST",
        "path": "/api/comfyui/workflows/import",
        "path_params": {},
        "body_fields": {"name", "description", "workflow", "layout", "metadata"},
        "required": {"name", "workflow"},
        "write": True,
    },
    "write_comfyui_workflow_update": {
        "label": "更新 ComfyUI workflow",
        "description": "更新 ComfyUI workflow preset。",
        "method": "PUT",
        "path": "/api/comfyui/workflows/{preset_id}",
        "path_params": {"preset_id": "positive_int"},
        "body_fields": {"name", "description", "workflow", "layout", "metadata"},
        "required": {"preset_id"},
        "write": True,
    },
    "write_comfyui_workflow_delete": {
        "label": "刪除 ComfyUI workflow",
        "description": "刪除 ComfyUI workflow preset。",
        "method": "DELETE",
        "path": "/api/comfyui/workflows/{preset_id}",
        "path_params": {"preset_id": "positive_int"},
        "body_fields": set(),
        "required": {"preset_id"},
        "write": True,
    },
    "write_comfyui_civitai_inspect": {
        "label": "檢查 Civitai 模型",
        "description": "root 檢查 Civitai 模型資訊。",
        "method": "POST",
        "path": "/api/root/comfyui/civitai/inspect",
        "path_params": {},
        "body_fields": {"url", "model_url", "version_id"},
        "required": set(),
        "write": True,
    },
    "write_comfyui_civitai_search": {
        "label": "搜尋 Civitai 模型",
        "description": "root 搜尋 Civitai 模型。",
        "method": "POST",
        "path": "/api/root/comfyui/civitai/search",
        "path_params": {},
        "body_fields": {"query", "model_type", "limit", "nsfw"},
        "required": {"query"},
        "write": True,
    },
    "write_comfyui_civitai_download": {
        "label": "下載 Civitai 模型",
        "description": "root 下載 Civitai 模型到站內 ComfyUI 模型目錄。",
        "method": "POST",
        "path": "/api/root/comfyui/civitai/download",
        "path_params": {},
        "body_fields": {"url", "download_url", "model_id", "version_id", "target_type", "filename"},
        "required": set(),
        "write": True,
    },
    "write_security_test_pentest": {
        "label": "執行安全滲透測試",
        "description": "root 執行站內安全滲透測試任務。",
        "method": "POST",
        "path": "/api/root/security-tests/pentest",
        "path_params": {},
        "body_fields": {"target", "profile", "tool_timeout_seconds", "reason"},
        "required": set(),
        "write": True,
    },
    "write_security_test_functional": {
        "label": "執行功能安全測試",
        "description": "root 執行站內功能安全測試任務。",
        "method": "POST",
        "path": "/api/root/security-tests/functional",
        "path_params": {},
        "body_fields": {"profile", "tool_timeout_seconds", "reason"},
        "required": set(),
        "write": True,
    },
    "write_security_test_privilege": {
        "label": "執行權限安全測試",
        "description": "root 執行站內權限安全測試任務。",
        "method": "POST",
        "path": "/api/root/security-tests/privilege",
        "path_params": {},
        "body_fields": {"profile", "tool_timeout_seconds", "reason"},
        "required": set(),
        "write": True,
    },
    "write_security_test_stress": {
        "label": "執行安全壓力測試",
        "description": "root 執行站內安全壓力測試任務。",
        "method": "POST",
        "path": "/api/root/security-tests/stress",
        "path_params": {},
        "body_fields": {"profile", "concurrency", "duration_seconds", "tool_timeout_seconds", "reason"},
        "required": set(),
        "write": True,
    },
})


def _actor_value(actor, key, default=None):
    if not actor:
        return default
    try:
        return actor[key]
    except Exception:
        return actor.get(key, default) if hasattr(actor, "get") else default


def register_ai_agent_routes(app, deps):
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_system_settings = deps.get("get_system_settings", lambda: {})
    get_client_ip = deps.get("get_client_ip", lambda: "")
    get_ua = deps.get("get_ua", lambda: "")
    get_db = deps["get_db"]
    get_audit_db = deps.get("get_audit_db", get_db)
    storage_root = deps.get("STORAGE_DIR", ".")
    fernet = deps.get("fernet")
    audit = deps.get("audit", lambda *args, **kwargs: None)
    json_resp = deps["json_resp"]
    require_csrf_safe = deps["require_csrf_safe"]
    require_csrf = deps.get("require_csrf", require_csrf_safe)
    role_rank = deps.get("role_rank", lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0))
    server_mode_service = deps.get("server_mode_service")

    def _clamp_float(value, minimum=0.0, maximum=100.0):
        try:
            parsed = float(value)
        except Exception:
            return None
        if parsed != parsed:
            return None
        return max(minimum, min(maximum, parsed))

    def _safe_percent(value):
        try:
            return _clamp_float(value)
        except Exception:
            return None

    def _read_meminfo_int(key):
        try:
            with open("/proc/meminfo", "r", encoding="utf-8") as file_obj:
                for line in file_obj:
                    if not line.startswith(f"{key}:"):
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        return None
                    return int(parts[1]) * 1024
        except Exception:
            return None
        return None

    def _resource_snapshot():
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
            used_ram = max(0, int(total_ram - available_ram))
            ram_percent = _safe_percent((used_ram / total_ram) * 100.0 if total_ram else None)
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
                "percent": disk_percent or 0,
            },
        }

    def _parse_json_field(raw):
        if isinstance(raw, dict):
            return raw
        if raw is None:
            return {}
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                return payload
        except Exception:
            return {}
        return {}

    def _row_value(row, key, default=None):
        if row is None:
            return default
        try:
            return row[key]
        except Exception:
            try:
                keys = list(row.keys())
            except Exception:
                keys = []
            if keys and key in keys:
                try:
                    return row[keys.index(key)]
                except Exception:
                    return default
            try:
                return row.get(key, default)
            except Exception:
                return default

    def _coerce_role(actor):
        actor_role = str(_actor_value(actor, "role") or "user").strip().lower()
        actor_name = str(_actor_value(actor, "username") or "").strip()
        if actor_name == "root":
            return "super_admin"
        if actor_role in {"manager", "admin", "super_admin", "user"}:
            return actor_role
        if actor_role in {"root", "super"}:
            return "super_admin"
        return "user"

    def _actor_scope_payload(actor):
        actor_role = _coerce_role(actor)
        rank = role_rank(actor_role)
        return {
            "role": actor_role,
            "level": rank,
            "can_manage_members": rank >= role_rank("manager"),
            "can_manage_servers": rank >= role_rank("super_admin"),
        }

    def _server_mode_payload(actor):
        actor_level = _actor_scope_payload(actor)
        if not actor_level["can_manage_servers"]:
            return {"ok": False, "msg": "需要 root 權限才能讀取伺服器模式。"}
        if not server_mode_service:
            return {"ok": False, "msg": "Server Mode 服務目前無法使用。"}
        payload = {
            "ok": True,
            "mode": server_mode_service.get_current_mode(),
            "profiles": server_mode_service.list_profiles(),
        }
        if hasattr(server_mode_service, "production_requirements"):
            payload["production_requirements"] = server_mode_service.production_requirements()
        if hasattr(server_mode_service, "incident_status"):
            payload["incident"] = server_mode_service.incident_status().get("incident")
        return payload

    def _table_exists(conn, table_name):
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table_name,),
            ).fetchone()
            return bool(row)
        except Exception:
            return False

    def _table_has_columns(conn, table_name, expected_columns):
        if not _table_exists(conn, table_name):
            return False
        try:
            cols = {str(c[1] if not hasattr(c, 'get') else c.get("name") or "") for c in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
            return set(expected_columns).issubset(cols)
        except Exception:
            return False

    def _safe_scalar_int(conn, sql, params, default=0):
        try:
            row = conn.execute(sql, params).fetchone()
            return int((row[0] if row else default) or default)
        except Exception:
            return default

    def _safe_scalar_text(conn, sql, params, default=""):
        try:
            row = conn.execute(sql, params).fetchone()
            value = row[0] if row else default
            return str(value or default)
        except Exception:
            return default

    def _safe_rows(conn, sql, params, limit=50):
        try:
            return conn.execute(sql, params).fetchall()
        except Exception:
            return []

    def _member_management_payload(actor, limit=50):
        actor_level = _actor_scope_payload(actor)
        if not actor_level["can_manage_members"]:
            return {}
        conn = get_db()
        try:
            if not _table_exists(conn, "users"):
                return {}
            total_users = _safe_scalar_int(conn, "SELECT COUNT(*) AS c FROM users", ())
            active_users = 0
            if _table_has_columns(conn, "users", ["status"]):
                active_users = _safe_scalar_int(conn, "SELECT COUNT(*) AS c FROM users WHERE COALESCE(status, 'active')='active'", ())

            role_rows = _safe_rows(
                conn,
                "SELECT role, COUNT(*) AS c FROM users WHERE role IS NOT NULL GROUP BY role ORDER BY c DESC LIMIT 20",
                (),
            )
            role_breakdown = []
            for row in role_rows:
                role_breakdown.append({
                    "role": str(_row_value(row, "role") or "") or str(row[0] if hasattr(row, "__iter__") else ""),
                    "count": int(_row_value(row, "c") or 0),
                })

            recent_users = []
            if _table_has_columns(conn, "users", ["id", "username", "created_at"]):
                recent_rows = _safe_rows(
                    conn,
                    "SELECT id, username, COALESCE(status, 'active') AS status, COALESCE(role, 'user') AS role, created_at\n                     FROM users ORDER BY created_at DESC LIMIT ?",
                    (min(8, max(1, limit // 6 + 1)),),
                )
                for row in recent_rows:
                    recent_users.append({
                        "id": int(_row_value(row, "id") or 0),
                        "username": _row_value(row, "username") or "",
                        "role": _row_value(row, "role") or "",
                        "status": _row_value(row, "status") or "",
                        "created_at": _row_value(row, "created_at") or "",
                    })

            new_users_24h = _safe_scalar_int(
                conn,
                "SELECT COUNT(*) AS c FROM users WHERE COALESCE(created_at, '') >= datetime('now', '-1 day')",
                (),
            ) if _table_has_columns(conn, "users", ["created_at"]) else 0

            return {
                "total_users": total_users,
                "active_users": active_users,
                "new_users_24h": new_users_24h,
                "role_breakdown": role_breakdown,
                "recent_users": recent_users[: limit],
            }
        finally:
            conn.close()

    def _attack_diagnosis_payload(actor, limit=50):
        actor_level = _actor_scope_payload(actor)
        if not actor_level["can_manage_servers"]:
            return {}
        conn = get_db()
        try:
            result = {
                "security_events": [],
                "recent_failed_jobs": [],
            }
            if _table_exists(conn, "security_events"):
                events = _safe_rows(
                    conn,
                    "SELECT event_type, ip_address, target_user, detail, created_at FROM security_events ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
                for row in events:
                    result["security_events"].append({
                        "type": _row_value(row, "event_type") or "",
                        "ip": _row_value(row, "ip_address") or "-",
                        "target": _row_value(row, "target_user") or "",
                        "detail": _row_value(row, "detail") or "",
                        "created_at": _row_value(row, "created_at") or "",
                    })

            if _table_exists(conn, "job_center_jobs"):
                failed = _safe_rows(
                    conn,
                    "SELECT job_uuid, owner_user_id, owner_username, status, error_code, error_message, stage, progress_percent, stage_detail, updated_at\n                     FROM job_center_jobs\n                     WHERE COALESCE(status, '') IN ('failed','cancelled','error')\n                     ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                )
                for row in failed:
                    result["recent_failed_jobs"].append({
                        "job_uuid": _row_value(row, "job_uuid") or "",
                        "status": _row_value(row, "status") or "failed",
                        "owner_user_id": int(_row_value(row, "owner_user_id") or 0),
                        "owner_username": _row_value(row, "owner_username") or "",
                        "stage": _row_value(row, "stage") or "",
                        "stage_detail": _row_value(row, "stage_detail") or "",
                        "error_code": _row_value(row, "error_code") or "",
                        "error_message": _row_value(row, "error_message") or "",
                        "progress_percent": int(_row_value(row, "progress_percent") or 0),
                        "updated_at": _row_value(row, "updated_at") or "",
                    })
            return result
        finally:
            conn.close()

    def _coerce_limit(raw):
        try:
            raw_int = int(raw)
        except Exception:
            return 20
        return max(1, min(100, raw_int))

    def _agent_list_comfyui_jobs(actor, limit=20):
        actor_id = int(_actor_value(actor, "id") or 0)
        if actor_id <= 0:
            return []
        conn = get_db()
        try:
            if not _table_exists(conn, "comfyui_generation_jobs"):
                return []
            rows = conn.execute(
                """
                SELECT job_id, owner_user_id, owner_username, status, error, progress_json, created_at, updated_at
                FROM comfyui_generation_jobs
                WHERE owner_user_id=?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (actor_id, limit),
            ).fetchall()
            result = []
            for row in rows:
                progress = _parse_json_field(_row_value(row, "progress_json"))
                result.append({
                    "job_id": _row_value(row, "job_id"),
                    "owner_user_id": int(_row_value(row, "owner_user_id") or 0),
                    "owner_username": _row_value(row, "owner_username") or "",
                    "status": _row_value(row, "status") or "queued",
                    "error": _row_value(row, "error") or "",
                    "progress_percent": _safe_percent(progress.get("percent") or 0),
                    "progress": {
                        "phase": progress.get("phase") or progress.get("stage") or "",
                        "detail": progress.get("detail") or progress.get("stage_detail") or "",
                    },
                    "created_at": _row_value(row, "created_at"),
                    "updated_at": _row_value(row, "updated_at"),
                })
            return result
        finally:
            conn.close()

    def _agent_list_remote_download_jobs(actor, limit=20):
        actor_id = int(_actor_value(actor, "id") or 0)
        if actor_id <= 0:
            return []
        conn = get_db()
        try:
            if not _table_exists(conn, "job_center_jobs"):
                return []
            cols = {
                str(c[1] if not hasattr(c, "get") else c.get("name") or "")
                for c in conn.execute("PRAGMA table_info(job_center_jobs)").fetchall()
            }
            created_at_select = ", created_at" if "created_at" in cols else ""
            rows = conn.execute(
                """
                SELECT job_uuid, status, stage, stage_detail, progress_percent, error_code, error_message,
                       metadata_json, result_json{created_at_select}, updated_at
                FROM job_center_jobs
                WHERE owner_user_id=? AND source_module='cloud_drive_remote_download'
                ORDER BY updated_at DESC
                LIMIT ?
                """.format(
                    created_at_select=created_at_select,
                ),
                (actor_id, limit),
            ).fetchall()
            result = []
            for row in rows:
                metadata = _parse_json_field(_row_value(row, "metadata_json"))
                result_json = _parse_json_field(_row_value(row, "result_json"))
                result.append({
                    "job_uuid": _row_value(row, "job_uuid"),
                    "status": _row_value(row, "status") or "queued",
                    "stage": _row_value(row, "stage") or "",
                    "stage_detail": _row_value(row, "stage_detail") or "",
                    "progress_percent": int(_row_value(row, "progress_percent") or 0),
                    "error_code": _row_value(row, "error_code") or "",
                    "error_message": _row_value(row, "error_message") or "",
                    "filename": metadata.get("filename") or result_json.get("filename") or "",
                    "loaded_bytes": int(metadata.get("loaded_bytes") or result_json.get("bytes") or 0),
                    "total_bytes": int(metadata.get("total_bytes") or 0),
                    "speed_bytes_per_sec": int(metadata.get("speed_bytes_per_sec") or 0),
                    "source_type": metadata.get("source_type") or "",
                    "created_at": _row_value(row, "created_at"),
                    "updated_at": _row_value(row, "updated_at"),
                })
            return result
        finally:
            conn.close()

    def _agent_list_storage_files(actor, limit=20):
        actor_id = int(_actor_value(actor, "id") or 0)
        if actor_id <= 0:
            return []
        actor_level = _actor_scope_payload(actor)
        conn = get_db()
        try:
            if not (_table_exists(conn, "storage_files") and _table_exists(conn, "uploaded_files")):
                return []
            where = "sf.deleted_at IS NULL AND f.deleted_at IS NULL AND COALESCE(f.system_asset_type, '')<>'avatar'"
            params = []
            if not actor_level["can_manage_servers"]:
                where += " AND sf.owner_user_id=?"
                params.append(actor_id)
            rows = conn.execute(
                f"""
                SELECT sf.id, sf.file_id, sf.owner_user_id, COALESCE(u.username, '') AS owner_username,
                       sf.display_name, sf.virtual_path, sf.is_trashed, sf.created_at, sf.updated_at,
                       f.size_bytes, f.privacy_mode, f.risk_level, f.scan_status, f.mime_type_plain_for_public
                FROM storage_files sf
                JOIN uploaded_files f ON f.id=sf.file_id
                LEFT JOIN users u ON u.id=sf.owner_user_id
                WHERE {where}
                ORDER BY sf.updated_at DESC, sf.created_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
            result = []
            for row in rows:
                result.append({
                    "id": _row_value(row, "id") or "",
                    "file_id": _row_value(row, "file_id") or "",
                    "owner_user_id": int(_row_value(row, "owner_user_id") or 0),
                    "owner_username": _row_value(row, "owner_username") or "",
                    "display_name": _row_value(row, "display_name") or "",
                    "virtual_path": _row_value(row, "virtual_path") or "",
                    "is_trashed": bool(_row_value(row, "is_trashed") or 0),
                    "size_bytes": int(_row_value(row, "size_bytes") or 0),
                    "privacy_mode": _row_value(row, "privacy_mode") or "",
                    "risk_level": _row_value(row, "risk_level") or "",
                    "scan_status": _row_value(row, "scan_status") or "",
                    "mime_type": _row_value(row, "mime_type_plain_for_public") or "",
                    "created_at": _row_value(row, "created_at") or "",
                    "updated_at": _row_value(row, "updated_at") or "",
                })
            return result
        finally:
            conn.close()

    def _actor_session_binding():
        raw = request.cookies.get("session_token") or ""
        raw = str(raw or "").strip()
        if not raw:
            return ""
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]

    def _actor_or_401():
        actor = get_current_user_ctx()
        if not actor:
            return None, json_resp({"ok": False, "msg": "請先登入"}, 401)
        user_id = int(_actor_value(actor, "id") or 0)
        if user_id <= 0:
            return None, json_resp({"ok": False, "msg": "無法辨識使用者身份"}, 401)
        settings = get_system_settings() or {}
        min_role = str(settings.get("module_ai_agent_min_role") or "user")
        actor_role = _coerce_role(actor)
        if _actor_value(actor, "username") != "root" and role_rank(actor_role) < role_rank(min_role):
            return None, json_resp({"ok": False, "msg": "沒有 AI Agent 使用權限"}, 403)
        return actor, None

    def _actor_is_manager_or_above(actor):
        actor_role = _coerce_role(actor)
        return role_rank(actor_role) >= role_rank("manager")

    def _actor_is_super_admin(actor):
        actor_role = _coerce_role(actor)
        return role_rank(actor_role) >= role_rank("super_admin")

    def _parse_bool(raw):
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            value = raw.strip().lower()
            if value in {"1", "true", "yes", "on", "y"}:
                return True
            if value in {"0", "false", "off", "no", "n"}:
                return False
        return None

    _os_filesystem_path_token_re = re.compile(
        r"(?<![\w.-])(?:~|/(?:home|root|etc|var|usr|tmp|proc|sys|dev|run|boot)"
        r"(?:/[^\s`'\"，,。；;:<>)]*)?)",
        re.IGNORECASE,
    )
    _os_filesystem_intent_re = re.compile(
        r"(列出|有哪些檔案|哪些檔案|檔案清單|資料夾|家目錄|目錄內容|讀取|查看|打開|顯示|"
        r"\bls\b|\bdir\b|\bcat\b|\bread\b|\bshow\b|\bopen\b|\blist(?:\s+(?:files|directory|folders?))?)",
        re.IGNORECASE,
    )
    _os_filesystem_mutation_intent_re = re.compile(
        r"(修改|改寫|覆寫|寫入|建立|新增|刪除|移除|清空|重命名|搬移|替換|patch|套用|編輯|"
        r"\bwrite\b|\bedit\b|\bmodify\b|\bdelete\b|\bremove\b|\bcreate\b|\boverwrite\b|"
        r"\btruncate\b|\brename\b|\bmove\b|\bpatch\b|\breplace\b)",
        re.IGNORECASE,
    )
    _write_tool_path_arg_names = {
        "path", "file_path", "filepath", "target_path", "source_path", "destination_path",
        "output_path", "input_path", "directory", "dir", "folder", "storage_path",
        "local_path", "server_path", "repo_path",
    }

    def _extract_ai_agent_user_text(data):
        prompt = str(data.get("prompt") or "").strip()
        if prompt:
            return prompt
        messages = data.get("messages")
        if not isinstance(messages, list):
            return ""
        user_texts = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            content = message.get("content")
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        parts.append(str(item.get("text") or item.get("content") or ""))
                    else:
                        parts.append(str(item or ""))
                text = "\n".join(part for part in parts if part)
            else:
                text = str(content or "")
            if "\nuser=" in text:
                text = text.rsplit("\nuser=", 1)[-1]
            if role in {"user", ""} and text.strip():
                user_texts.append(text.strip())
        return "\n".join(user_texts[-3:])

    def _ai_agent_runtime_roots():
        candidates = []
        env_runtime = str(os.environ.get("HACKME_RUNTIME_DIR") or "").strip()
        if env_runtime:
            candidates.append(env_runtime)
        repo_runtime = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "runtime"))
        candidates.append(repo_runtime)
        cwd_runtime = os.path.abspath(os.path.join(os.getcwd(), "runtime"))
        candidates.append(cwd_runtime)
        roots = []
        seen = set()
        for item in candidates:
            try:
                normalized = os.path.abspath(os.path.expanduser(str(item or "")))
            except Exception:
                continue
            if normalized and normalized not in seen:
                roots.append(normalized)
                seen.add(normalized)
        return roots

    def _ai_agent_path_is_allowed_runtime(path_text):
        raw = str(path_text or "").strip()
        if not raw:
            return False
        try:
            normalized = os.path.abspath(os.path.expanduser(raw))
        except Exception:
            return False
        for root in _ai_agent_runtime_roots():
            if normalized == root or normalized.startswith(root.rstrip(os.sep) + os.sep):
                return True
        parts = [part for part in normalized.split(os.sep) if part]
        return normalized.startswith("/tmp/hackme") and "runtime" in parts

    def _ai_agent_os_paths_outside_runtime(text):
        paths = []
        for match in _os_filesystem_path_token_re.finditer(str(text or "")):
            token = match.group(0).rstrip(".")
            if token and not _ai_agent_path_is_allowed_runtime(token):
                paths.append(token)
        return paths

    def _ai_agent_boundary_block_reason(user_text):
        text = str(user_text or "").strip()
        if not text:
            return ""
        outside_runtime_paths = _ai_agent_os_paths_outside_runtime(text)
        if outside_runtime_paths and _os_filesystem_mutation_intent_re.search(text):
            return "server_filesystem_mutation"
        if outside_runtime_paths and _os_filesystem_intent_re.search(text):
            return "filesystem_scope"
        return ""

    def _ai_agent_write_tool_boundary_block_reason(tool_name, args):
        if not isinstance(args, dict):
            return ""
        for key, value in args.items():
            key_name = str(key or "").strip().lower()
            if key_name not in _write_tool_path_arg_names:
                continue
            if isinstance(value, (dict, list, tuple)):
                continue
            if _ai_agent_os_paths_outside_runtime(str(value or "")):
                return f"server_filesystem_arg:{tool_name}:{key_name}"
        return ""

    def _audit_agent_event(action, actor=None, *, success=True, detail=""):
        audit(
            action,
            get_client_ip(),
            user=_actor_value(actor, "username", "-"),
            ua=get_ua(),
            success=success,
            detail=str(detail or "")[:500],
        )

    def _write_tool_domain(name, spec):
        blueprint = AI_AGENT_TOOL_BLUEPRINT.get(name) or {}
        scope = str(blueprint.get("data_scope") or "").strip()
        if scope.startswith("write_tool:"):
            return scope.split(":", 1)[1] or "general"
        if name == "audit_scan":
            return "audit"
        if name.startswith("write_"):
            parts = name.split("_")
            if len(parts) >= 2 and parts[1]:
                return parts[1]
        return "general"

    def _write_tool_public_spec(name, spec):
        blueprint = AI_AGENT_TOOL_BLUEPRINT.get(name) or {}
        return {
            "name": name,
            "label": spec.get("label") or name,
            "description": spec.get("description") or "",
            "data_scope": blueprint.get("data_scope") or "",
            "domain": _write_tool_domain(name, spec),
            "arg_hint": AI_AGENT_TOOL_ARGUMENT_HINTS.get(name, ""),
            "method": spec.get("method") if spec.get("method") != "DIRECT" else "POST",
            "required": sorted(spec.get("required") or []),
            "path_params": sorted((spec.get("path_params") or {}).keys()),
            "body_fields": sorted(spec.get("body_fields") or []),
            "query_fields": sorted(spec.get("query_fields") or []),
            "write": bool(spec.get("write")),
            "root_only": True,
            "requires_confirm": bool(spec.get("write")),
        }

    def _write_tool_catalog_fingerprint(tools=None):
        if tools is None:
            tools = [
                _write_tool_public_spec(name, spec)
                for name, spec in AI_AGENT_WRITE_TOOL_SPECS.items()
            ]
        canonical = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _write_tool_effective_names(settings, actor):
        public = public_ai_agent_settings(settings, actor=actor)
        return {
            str(tool.get("name") or "")
            for tool in public.get("tools") or []
            if tool.get("name")
        }

    def _require_write_tool_actor():
        actor, denied = _actor_or_401()
        if denied:
            return None, denied
        if not _actor_is_super_admin(actor):
            _audit_agent_event("AI_AGENT_WRITE_TOOLS_DENIED", actor, success=False, detail="root_only")
            return None, (json_resp({"ok": False, "msg": "write-tool endpoint 目前僅開放 root"}), 403)
        return actor, None

    def _ai_agent_write_guard_denied(actor, *, endpoint):
        guard = ai_agent_write_guard_status(get_db=get_audit_db)
        if not guard.get("blocked"):
            return None
        detail = f"endpoint={endpoint},reason={str(guard.get('reason') or '')[:180]}"
        _audit_agent_event("AI_AGENT_WRITE_TOOLS_LOCKDOWN", actor, success=False, detail=detail)
        return json_resp({
            "ok": False,
            "msg": "AI Agent audit 已偵測異常，write-tools 已暫停，請 root 檢查 audit log 後重新審計。",
            "guard": guard,
        }), 423

    def _request_json_dict():
        try:
            data = request.get_json(force=True)
        except Exception:
            return None, (json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400)
        if not isinstance(data, dict):
            return None, (json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400)
        return data, None

    def _run_ai_agent_chat_with_timeout(timeout_seconds, **kwargs):
        timeout_seconds = max(5, min(610, int(timeout_seconds or 120)))
        if not AI_AGENT_CHAT_WORKERS.acquire(blocking=False):
            raise AiAgentError(
                "AI Agent chat 執行槽已滿，請稍後重試",
                http_status=503,
            )
        box = {}

        def worker():
            try:
                box["result"] = ai_agent_chat(**kwargs)
            except BaseException as exc:
                box["exc"] = exc
            finally:
                try:
                    AI_AGENT_CHAT_WORKERS.release()
                except ValueError:
                    pass

        thread = threading.Thread(target=worker, name="ai-agent-chat-request", daemon=True)
        thread.start()
        thread.join(timeout_seconds)
        if thread.is_alive():
            box["timed_out"] = True
            return None, True
        if "exc" in box:
            raise box["exc"]
        return box.get("result"), False

    def _ensure_ai_agent_conversation_schema(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_agent_conversations (
                owner_user_id INTEGER NOT NULL,
                session_binding TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                payload_encrypted TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (owner_user_id, session_binding, conversation_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_agent_conversations_owner_updated "
            "ON ai_agent_conversations(owner_user_id, updated_at)"
        )

    def _ensure_ai_agent_codex_handoff_schema(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ai_agent_codex_handoffs (
                id TEXT PRIMARY KEY,
                owner_user_id INTEGER NOT NULL,
                owner_username TEXT NOT NULL,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                title TEXT NOT NULL,
                objective TEXT NOT NULL,
                context_json TEXT NOT NULL,
                allowed_scope TEXT NOT NULL,
                requested_artifacts_json TEXT NOT NULL,
                safety_notes TEXT NOT NULL,
                source_conversation_id TEXT,
                source_session_binding TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_agent_codex_handoffs_owner_updated "
            "ON ai_agent_codex_handoffs(owner_user_id, updated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ai_agent_codex_handoffs_status_updated "
            "ON ai_agent_codex_handoffs(status, updated_at)"
        )

    def _conversation_binding():
        return _actor_session_binding() or "sessionless"

    def _conversation_id(raw):
        value = str(raw or "default").strip()[:120] or "default"
        value = re.sub(r"[^0-9A-Za-z_.:-]", "_", value)
        return value[:120] or "default"

    def _sanitize_conversation_payload(data):
        if not isinstance(data, dict):
            data = {}
        messages = data.get("messages")
        if not isinstance(messages, list):
            messages = []
        cleaned = []
        for message in messages[-80:]:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip()
            if role not in {"user", "assistant"}:
                continue
            images = []
            raw_images = message.get("images") if isinstance(message.get("images"), list) else []
            for image in raw_images[:4]:
                if not isinstance(image, dict):
                    continue
                image_ref = image.get("image_ref")
                if not isinstance(image_ref, dict):
                    continue
                images.append({
                    "image_ref": image_ref,
                    "cloud_file_id": str(image.get("cloud_file_id") or image_ref.get("cloud_file_id") or "")[:160],
                    "storage_file_id": str(image.get("storage_file_id") or image_ref.get("storage_file_id") or "")[:160],
                    "prompt_id": str(image.get("prompt_id") or "")[:160],
                    "filename": str(image.get("filename") or "")[:260],
                    "mime_type": str(image.get("mime_type") or "")[:80],
                })
            cleaned.append({
                "role": role,
                "content": str(message.get("content") or "")[:20000],
                "images": images,
            })
        habits = data.get("habits") if isinstance(data.get("habits"), dict) else {}
        safe_habits = {
            str(key)[:80]: str(value)[:1000]
            for key, value in list(habits.items())[:40]
        }
        return {
            "sessionId": str(data.get("sessionId") or data.get("session_id") or "")[:120],
            "messages": cleaned,
            "habits": safe_habits,
        }

    def _encrypt_conversation_payload(payload):
        if not fernet:
            raise ValueError("AI Agent encrypted memory key is unavailable")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
        return fernet.encrypt(raw.encode("utf-8")).decode("utf-8")

    def _decrypt_conversation_payload(value):
        if not fernet:
            raise ValueError("AI Agent encrypted memory key is unavailable")
        raw = fernet.decrypt(str(value or "").encode("utf-8")).decode("utf-8")
        parsed = json.loads(raw)
        return _sanitize_conversation_payload(parsed if isinstance(parsed, dict) else {})

    def _conversation_preview(payload):
        messages = payload.get("messages") if isinstance(payload, dict) else []
        if not isinstance(messages, list):
            messages = []
        last_user = ""
        last_assistant = ""
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "")
            content = " ".join(str(message.get("content") or "").split())[:240]
            if role == "user" and not last_user:
                last_user = content
            elif role == "assistant" and not last_assistant:
                last_assistant = content
            if last_user and last_assistant:
                break
        return {
            "message_count": len(messages),
            "last_user": last_user,
            "last_assistant": last_assistant,
        }

    def _conversation_history_row(row, *, include_payload=False):
        payload = _decrypt_conversation_payload(_row_value(row, "payload_encrypted"))
        preview = _conversation_preview(payload)
        result = {
            "owner_user_id": int(_row_value(row, "owner_user_id") or 0),
            "owner_username": _row_value(row, "owner_username") or "",
            "session_binding": _row_value(row, "session_binding") or "",
            "conversation_id": _row_value(row, "conversation_id") or "default",
            "created_at": _row_value(row, "created_at") or "",
            "updated_at": _row_value(row, "updated_at") or "",
            "message_count": preview["message_count"],
            "last_user": preview["last_user"],
            "last_assistant": preview["last_assistant"],
        }
        if include_payload:
            result["payload"] = payload
        return result

    def _is_missing_arg(value):
        return value is None or (isinstance(value, str) and not value.strip())

    def _coerce_write_path_param(name, value, kind):
        if kind == "positive_int":
            try:
                parsed = int(value)
            except Exception:
                return None, f"{name} 必須是正整數"
            if parsed <= 0:
                return None, f"{name} 必須是正整數"
            return parsed, ""
        if kind == "safe_id":
            raw = str(value or "").strip()
            if not raw or len(raw) > 120:
                return None, f"{name} 格式錯誤"
            allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:")
            if any(ch not in allowed for ch in raw):
                return None, f"{name} 只能包含英數、底線、減號、冒號或點"
            return raw, ""
        if kind == "safe_path":
            raw = str(value or "").strip()
            if not raw or len(raw) > 180:
                return None, f"{name} 格式錯誤"
            if raw.startswith("/") or "?" in raw or "#" in raw or "\\" in raw:
                return None, f"{name} 不可包含 URL 跳脫字元"
            parts = [part for part in raw.split("/") if part]
            if not parts or any(part in {".", ".."} for part in parts):
                return None, f"{name} 不可包含相對跳脫"
            allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.:/")
            if any(ch not in allowed for ch in raw):
                return None, f"{name} 只能包含英數、底線、減號、冒號、點或斜線"
            return raw, ""
        return str(value or "").strip(), ""

    def _validate_launch_doc_path(raw):
        value = str(raw or "").strip()
        if not value.startswith("docs/") or not value.endswith(".md"):
            return None, "path 只允許 docs/ 內的 Markdown 文件"
        parts = [part for part in value.split("/") if part]
        if any(part in {".", ".."} for part in parts):
            return None, "path 不可包含相對跳脫"
        return value, ""

    def _comfyui_model_match_key(value):
        text = str(value or "").strip().lower().replace("\\", "/")
        text = text.rsplit("/", 1)[-1]
        text = re.sub(r"\.(?:safetensors?|ckpt|pt|pth|bin|gguf)$", "", text)
        return re.sub(r"[^0-9a-z]+", "", text)

    def _comfyui_model_query_tokens(value):
        text = str(value or "").strip().lower().replace("\\", "/")
        text = text.rsplit("/", 1)[-1]
        text = re.sub(r"\.(?:safetensors?|ckpt|pt|pth|bin|gguf)$", "", text)
        return [
            token
            for token in re.split(r"[^0-9a-z]+", text)
            if token and token not in {"model", "checkpoint", "ckpt", "safetensor", "safetensors"}
        ]

    def _is_generic_sdxl_checkpoint_request(value):
        tokens = _comfyui_model_query_tokens(value)
        if not tokens:
            return False
        allowed = {"sdxl", "sd", "xl", "base", "1", "0", "10", "t2i", "txt2img", "text", "to", "image", "default"}
        if not set(tokens).issubset(allowed):
            return False
        key = _comfyui_model_match_key(value)
        return bool(
            key in {
                "sdxl",
                "sdxlbase",
                "sdxlbase1",
                "sdxlbase10",
                "sdxl10",
                "sdxlt2i",
                "sdxltxt2img",
                "sdxltexttoimage",
                "sdxldefault",
            }
            or key.startswith("sdxlbase")
        )

    def _preferred_comfyui_checkpoint_option(model_options):
        options = [
            str(option or "").strip()
            for option in (model_options or [])
            if str(option or "").strip()
        ]
        if not options:
            return ""
        configured_default = str(os.environ.get("HACKME_AI_AGENT_DEFAULT_COMFYUI_CHECKPOINT") or "").strip()
        if configured_default:
            configured_key = _comfyui_model_match_key(configured_default)
            configured_matches = [
                option
                for option in options
                if option == configured_default or _comfyui_model_match_key(option) == configured_key
            ]
            if len(set(configured_matches)) == 1:
                return configured_matches[0]
        preferred_terms = (
            ("jankutrainedchenkinnoobai", 90),
            ("janku", 80),
            ("noob", 70),
            ("illustrious", 60),
            ("ilxl", 55),
            ("perfectionrealistic", 50),
            ("sdxl", 40),
            ("xl", 20),
        )
        def version_score(option):
            key = _comfyui_model_match_key(option)
            versions = [int(match) for match in re.findall(r"(?:^|v)(\d{1,5})(?=$|[a-z])", key)]
            if not versions:
                versions = [int(match) for match in re.findall(r"\d{1,5}", key)]
            return max(versions) if versions else 0
        scored = []
        for index, option in enumerate(options):
            key = _comfyui_model_match_key(option)
            score = sum(weight for term, weight in preferred_terms if term in key)
            scored.append((score, version_score(option), -index, option))
        scored.sort(reverse=True)
        return scored[0][3]

    def _resolve_comfyui_checkpoint_name(raw_name, model_options):
        requested = str(raw_name or "").strip()
        if not requested:
            return "", "", []
        options = [
            str(option or "").strip()
            for option in (model_options or [])
            if str(option or "").strip()
        ]
        if not options:
            return requested, "", []
        if _is_generic_sdxl_checkpoint_request(requested):
            preferred = _preferred_comfyui_checkpoint_option(options)
            if preferred:
                return preferred, "", []
        for option in options:
            if option == requested:
                return option, "", []
        requested_path = requested.replace("\\", "/").lower()
        for option in options:
            if option.replace("\\", "/").lower() == requested_path:
                return option, "", []
        requested_base = requested_path.rsplit("/", 1)[-1]
        exact_base = [
            option
            for option in options
            if option.replace("\\", "/").lower().rsplit("/", 1)[-1] == requested_base
        ]
        if len(set(exact_base)) == 1:
            return exact_base[0], "", []

        requested_key = _comfyui_model_match_key(requested)
        keyed = [
            option
            for option in options
            if _comfyui_model_match_key(option) == requested_key
        ] if requested_key else []
        if len(set(keyed)) == 1:
            return keyed[0], "", []

        tokens = _comfyui_model_query_tokens(requested)
        token_matches = []
        if tokens:
            for option in options:
                option_key = _comfyui_model_match_key(option)
                if all(token in option_key for token in tokens):
                    token_matches.append(option)
        unique_matches = sorted(set(token_matches))
        if len(unique_matches) == 1:
            return unique_matches[0], "", unique_matches
        if unique_matches:
            preview = "、".join(unique_matches[:8])
            return "", f"模型名稱「{requested}」符合多個 checkpoint，請指定完整名稱：{preview}", unique_matches

        preview = "、".join(options[:8])
        return "", f"模型名稱「{requested}」不在 ComfyUI checkpoint 清單中。可用模型：{preview}", []

    def _normalize_comfyui_generation_mode(value):
        raw = str(value or "").strip().lower()
        if not raw:
            return ""
        key = re.sub(r"[\s_-]+", "", raw)
        aliases = {
            "texttoimage": "txt2img",
            "txt2img": "txt2img",
            "t2i": "txt2img",
            "文字生圖": "txt2img",
            "imagetoimage": "img2img",
            "img2img": "img2img",
            "i2i": "img2img",
            "style": "img2img",
            "styletransfer": "img2img",
            "restyle": "img2img",
            "風格化": "img2img",
            "改風格": "img2img",
            "inpaint": "inpaint",
            "inpainting": "inpaint",
            "局部重繪": "inpaint",
            "局部修改": "inpaint",
            "outpaint": "outpaint",
            "outpainting": "outpaint",
            "外延": "outpaint",
            "向外延展": "outpaint",
            "upscale": "upscale",
            "放大修復": "upscale",
        }
        return aliases.get(key, raw)

    def _first_present_arg(args, names):
        for name in names:
            value = args.get(name)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
        return None

    def _image_ref_semantic_stage(ref):
        if not isinstance(ref, dict):
            return ""
        text = " ".join(
            str(ref.get(key) or "")
            for key in ("semantic_key", "context", "filename", "name", "label", "description")
        ).lower()
        if not text.strip():
            return ""
        if re.search(r"\b(clothes|clothing|outfit|garment|uniform|kimono|swimsuit|bikini|sailor)\b|服裝|衣服", text):
            return "clothes"
        if re.search(r"\b(chara|character|face|identity|hair|eyes?)\b|角色|臉|髮", text):
            return "chara"
        if re.search(r"\b(pose|posture|gesture|kneel|sit|stand|lying|arms?|legs?)\b|姿勢|動作", text):
            return "pose"
        if re.search(r"\b(background|scene|scenery|environment|location|lighting)\b|背景|場景|環境", text):
            return "background"
        return ""

    def _qwen_single_reference_stage_instruction(stage):
        if stage == "clothes":
            return (
                "use the extracted reference clothing traits only for outfit design, garment silhouette, colors, and clothing details; "
                "change only the source girl's clothes to match those clothing traits; preserve the source face, identity, "
                "hair color, hairstyle, expression, pose, hands, body proportions, composition, and background; "
                "do not copy the reference identity, pose, face, hair, hairstyle, hair color, cat ears, animal ears, hair accessories, or background; "
                "fit the outfit naturally with no body or cloth penetration."
            )
        if stage == "chara":
            return (
                "use the extracted reference character traits only for character appearance, face mood, eye shape, hair direction, and color cues; "
                "preserve the source outfit, pose, body, composition, and background; do not copy the reference clothing or background."
            )
        if stage == "pose":
            return (
                "use the reference image only for body pose, limb placement, and composition; preserve the source identity, "
                "face, hairstyle, clothing, colors, and background as much as possible; do not copy the reference identity, outfit, or background."
            )
        if stage == "background":
            return (
                "use the extracted reference background traits only for scene, setting, location, lighting, depth, and environmental details; "
                "change only the source background/scene to match those traits; preserve the source girl, face, identity, "
                "hair color, hairstyle, expression, clothing, pose, hands, body proportions, and foreground subject; "
                "do not copy reference people, identity, outfit, pose, text, signage words, watermark, logo, or signature."
            )
        return ""

    def _qwen_instruction_wrong_for_single_reference_stage(instruction, stage):
        text = str(instruction or "").lower()
        if not text.strip() or not stage:
            return False
        if stage == "clothes":
            if re.search(r"\b(face identity|different anime character face|eye shape|mature face)\b", text):
                return True
            has_clothes_action = re.search(
                r"\b(change|replace|edit|modify|turn|convert)\b.{0,80}\b(clothes|clothing|outfit|garment|dress|uniform|kimono|swimsuit|bikini|sailor)\b",
                text,
            )
            stale_other = re.search(r"\b(hair color|hairstyle|body pose|pose)\b", text)
            return bool(stale_other and not has_clothes_action)
        if stage == "chara":
            has_chara = re.search(r"\b(face|identity|character|hair|eye|appearance)\b", text)
            stale_other = re.search(r"\b(clothes|clothing|outfit|garment|body pose|pose)\b", text)
            return bool(stale_other and not has_chara)
        if stage == "pose":
            has_pose = re.search(r"\b(pose|posture|body|arm|leg|hand|composition|kneel|sit|stand|lying)\b", text)
            stale_other = re.search(r"\b(face|identity|clothes|clothing|outfit|garment)\b", text)
            return bool(stale_other and not has_pose)
        if stage == "background":
            has_background = re.search(r"\b(background|scene|scenery|environment|location|lighting|atmosphere)\b", text)
            stale_other = re.search(r"\b(face|identity|hair|hairstyle|clothes|clothing|outfit|garment|pose|posture)\b", text)
            return bool(stale_other and not has_background)
        return False

    def _normalize_qwen_single_reference_instruction(normalized):
        workflow_id = str(
            normalized.get("official_workflow_id")
            or normalized.get("workflow_id")
            or normalized.get("template_id")
            or ""
        ).strip()
        mode = str(normalized.get("generation_mode") or "").strip().lower()
        if not (_is_qwen_edit_workflow_id(workflow_id) or mode == "img2img"):
            return
        reference_ref = normalized.get("reference_image_ref")
        stage = _image_ref_semantic_stage(reference_ref)
        if not stage:
            return
        current_instruction = str(
            normalized.get("edit_instruction")
            or normalized.get("edit_prompt")
            or ""
        ).strip()
        if current_instruction and _qwen_instruction_wrong_for_single_reference_stage(current_instruction, stage):
            instruction = _qwen_single_reference_stage_instruction(stage)
            if instruction:
                normalized["edit_instruction"] = instruction
                normalized.pop("edit_prompt", None)

    def _strip_qwen_single_semantic_reference_image(body):
        workflow_id = _official_workflow_id_from_body(body)
        if not _is_qwen_edit_workflow_id(workflow_id):
            return body, []
        reference_ref = _image_ref_from_body(body, "reference_image_ref")
        if not isinstance(reference_ref, dict) or not str(reference_ref.get("semantic_key") or "").strip():
            return body, []
        stage = _image_ref_semantic_stage(reference_ref)
        if stage not in {"chara", "clothes", "background", "pose"}:
            return body, []
        reference_mode = str((body or {}).get("qwen_reference_mode") or "").strip().lower()
        instruction_text = str(
            (body or {}).get("edit_instruction")
            or (body or {}).get("edit_prompt")
            or ""
        ).lower()
        guarded_contract = (
            stage in {"chara", "clothes", "background"}
            and "use the reference image only" in instruction_text
            and "preserve the source" in instruction_text
            and "do not copy the reference" in instruction_text
        )
        explicit_image2_flag = "qwen_reference_image2" in (body or {})
        text_traits_only = (
            reference_mode in {"text_traits_only", "vision_text_traits_only", "reference_text_traits_only"}
            or (explicit_image2_flag and not bool((body or {}).get("qwen_reference_image2")))
        )
        if text_traits_only:
            next_body = dict(body or {})
            next_body.pop("reference_image_ref", None)
            next_body.pop("reference_image_ref_json", None)
            next_body.pop("pose_reference_image_ref", None)
            return next_body, [{
                "code": "qwen_single_reference_image2_text_traits_only",
                "stage": stage,
                "reason": "vision extracted the active reference traits; image2 is disabled to avoid low-VRAM stalls and reference leakage",
            }]
        force_image2 = bool((body or {}).get("qwen_reference_force_image2"))
        allow_guarded_image2 = (
            bool((body or {}).get("qwen_reference_image2"))
            or reference_mode in {"stage_guarded_image2", "guarded_image2", "image2_stage_guarded"}
        )
        if force_image2 and allow_guarded_image2:
            return body, [{
                "code": "qwen_single_reference_image2_force_guarded",
                "stage": stage,
                "reason": "operator explicitly forced image2 for this staged reference role",
            }]
        if allow_guarded_image2 and stage in {"chara", "clothes", "background"}:
            return body, [{
                "code": "qwen_single_reference_image2_stage_guarded" if not guarded_contract else "qwen_single_reference_image2_contract_guarded",
                "stage": stage,
                "reason": "stage contract explicitly restricts image2 to the active reference role",
            }]
        next_body = dict(body or {})
        next_body.pop("reference_image_ref", None)
        next_body.pop("reference_image_ref_json", None)
        next_body.pop("pose_reference_image_ref", None)
        return next_body, [{
            "code": "qwen_single_reference_image2_stripped",
            "stage": stage,
            "reason": "single semantic reference is used for agent/vision text traits only; direct image2 over-copies identity/background",
        }]

    def _normalize_comfyui_write_args(args):
        normalized = dict(args or {})
        prompt_text = str(
            normalized.get("prompt")
            or normalized.get("edit_instruction")
            or normalized.get("edit_prompt")
            or normalized.get("description")
            or ""
        )
        raw_mode = _first_present_arg(normalized, ("generation_mode", "mode", "edit_mode", "image_edit_mode", "task_mode"))
        mode_key = re.sub(r"[\s_-]+", "", str(raw_mode or "").strip().lower())
        mode = _normalize_comfyui_generation_mode(raw_mode)
        if mode:
            normalized["generation_mode"] = mode
        requested_workflow_id = str(normalized.get("official_workflow_id") or "").strip()
        wants_anything2real = bool(re.search(
            r"anything\s*2\s*real|anything2real|realistic\s+photograph|photoreal",
            prompt_text,
            re.IGNORECASE,
        ))
        wants_legacy_qwen_edit_mode = mode_key in {
            "style",
            "styletransfer",
            "restyle",
            "風格化",
            "改風格",
            "edit",
            "imageedit",
            "semanticedit",
        }
        if wants_anything2real and requested_workflow_id in {"", "origin_qwen_image_edit_2509"}:
            normalized["official_workflow_id"] = "origin_qwen_image_edit_2509_anything2real"
        elif not requested_workflow_id:
            if re.search(r"\bqwen\s+image\s+edit\b|\bqwen\s+edit\b|qwen\s*image\s*edit\s*2509", prompt_text, re.IGNORECASE):
                normalized["official_workflow_id"] = "origin_qwen_image_edit_2509"
            elif wants_legacy_qwen_edit_mode:
                normalized["official_workflow_id"] = "origin_qwen_image_edit_2509"
        size_match = re.search(r"(?<!\d)([1-9]\d{2,4})\s*[x×]\s*([1-9]\d{2,4})(?!\d)", prompt_text, re.IGNORECASE)
        if size_match:
            if normalized.get("width") is None:
                normalized["width"] = int(size_match.group(1))
            if normalized.get("height") is None:
                normalized["height"] = int(size_match.group(2))
        if _first_present_arg(normalized, ("denoise_strength", "denoise", "strength")) is None:
            denoise_match = re.search(
                r"\bdenoise(?:[_\s-]*strength)?\s*[:=]\s*(0(?:\.\d+)?|1(?:\.0+)?)\b",
                prompt_text,
                re.IGNORECASE,
            )
            if denoise_match:
                try:
                    denoise_value = float(denoise_match.group(1))
                except (TypeError, ValueError):
                    denoise_value = None
                if denoise_value is not None and 0 <= denoise_value <= 1:
                    normalized["denoise_strength"] = denoise_value
        if not str(normalized.get("official_workflow_id") or "").strip():
            shortcut_workflow = AI_AGENT_COMFYUI_SHORTCUT_WORKFLOWS.get(mode)
            if shortcut_workflow:
                normalized["official_workflow_id"] = shortcut_workflow
        if not normalized.get("cfg") and normalized.get("cfg_scale") is not None:
            normalized["cfg"] = normalized.get("cfg_scale")
        if not normalized.get("sampler_name") and normalized.get("sampler") is not None:
            normalized["sampler_name"] = normalized.get("sampler")
        source_ref = _first_present_arg(normalized, (
            "source_image_ref", "source_image_ref_json", "image_ref", "source_ref",
            "source_image", "input_image_ref", "previous_image_ref",
        ))
        if source_ref is not None:
            normalized["source_image_ref"] = source_ref
        mask_ref = _first_present_arg(normalized, (
            "mask_image_ref", "mask_image_ref_json", "mask_ref", "mask_image",
            "inpaint_mask_ref",
        ))
        if mask_ref is not None:
            normalized["mask_image_ref"] = mask_ref
        reference_ref = _first_present_arg(normalized, (
            "reference_image_ref", "reference_image_ref_json", "reference_ref",
            "pose_reference_image_ref", "pose_reference_ref", "pose_ref",
        ))
        if reference_ref is not None:
            normalized["reference_image_ref"] = reference_ref
        control_ref = _first_present_arg(normalized, (
            "control_image_ref", "control_image_ref_json", "control_ref",
            "controlnet_image_ref", "controlnet_ref", "pose_map_image_ref", "pose_map_ref",
        ))
        if control_ref is not None:
            normalized["control_image_ref"] = control_ref
            control = normalized.get("controlnet") if isinstance(normalized.get("controlnet"), dict) else {}
            control = dict(control)
            control.setdefault("image_ref", control_ref)
            normalized["controlnet"] = control
        denoise = _first_present_arg(normalized, ("denoise_strength", "denoise", "strength"))
        if denoise is not None:
            normalized["denoise_strength"] = denoise
        outpaint = normalized.get("outpaint")
        if isinstance(outpaint, str):
            try:
                outpaint = json.loads(outpaint)
            except Exception:
                outpaint = None
        if isinstance(outpaint, dict):
            for key in ("left", "top", "right", "bottom", "feathering"):
                field = f"outpaint_{key}"
                if normalized.get(field) is None and outpaint.get(key) is not None:
                    normalized[field] = outpaint.get(key)
        expand = _first_present_arg(normalized, ("outpaint_pixels", "outpaint_expand", "expand_pixels"))
        if expand is not None:
            for field in ("outpaint_left", "outpaint_top", "outpaint_right", "outpaint_bottom"):
                if normalized.get(field) is None:
                    normalized[field] = expand
        _normalize_qwen_edit_inline_instruction(normalized)
        _normalize_qwen_single_reference_instruction(normalized)
        return normalized

    def _extract_inline_edit_instruction(prompt):
        text = str(prompt or "").strip()
        if not text:
            return ""
        match = re.search(
            r"(?:use\s+a\s+short\s+english\s+edit\s+instruction\s+internally|"
            r"short\s+english\s+edit\s+instruction|internal\s+edit\s+instruction)"
            r"\s*[:：]\s*(.+?)\s*$",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return ""
        instruction = re.sub(r"\s+", " ", match.group(1)).strip(" \t\r\n\"'`")
        instruction = re.split(
            r"(?:解析度|分辨率|尺寸|batch\s*\d*|steps\s*\d*|cfg\s*\d*|confirm_billing|LoRA\s+strength|提示詞基礎|source\s+使用)",
            instruction,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" \t\r\n\"'`。．;；")
        return instruction[:1200]

    def _extract_prompt_style_context(prompt):
        text = str(prompt or "").strip()
        if not text:
            return ""
        match = re.search(
            r"(?:提示詞基礎|基礎提示詞|prompt\s*base|base\s*prompt|style\s*prompt)"
            r"\s*[:：]\s*([^。．\n\r；;]+)",
            text,
            re.IGNORECASE,
        )
        if match:
            style = re.sub(r"\s+", " ", match.group(1)).strip(" \t\r\n\"'`")
            if style:
                return style[:500]
        if re.search(r"\bby\s+ogipote\b", text, re.IGNORECASE):
            parts = []
            for phrase in ("by ogipote", "anime style", "1girl"):
                if re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE):
                    parts.append(phrase)
            return ", ".join(parts) or "by ogipote, anime style, 1girl"
        return ""

    QWEN_EDIT_STYLE_FALLBACK = (
        "by ogipote, anime style, 1girl, style tag only, do not render words, "
        "no visible text, no watermark, no signature, no logo, no visible artist name"
    )

    def _looks_like_qwen_edit_instruction_text(text):
        return bool(re.search(
            r"stage\s+\d+|merge:|visibly change|direct text edit|source character|"
            r"reference traits|target traits|apply these reference traits|current pairwise stage|"
            r"use this reference only|agent_review|vision gate|candidate",
            str(text or ""),
            re.IGNORECASE,
        ))

    def _sanitize_qwen_edit_style_context(style_context):
        raw = str(style_context or "").strip()
        marker_match = re.search(r"Style and preservation context\s*:\s*([\s\S]+)$", raw, re.IGNORECASE)
        if marker_match:
            marker_style = re.sub(r"\s+", " ", marker_match.group(1)).strip(" \t\r\n\"'`")
            style = marker_style if marker_style and not _looks_like_qwen_edit_instruction_text(marker_style) else QWEN_EDIT_STYLE_FALLBACK
        elif _looks_like_qwen_edit_instruction_text(raw):
            style = QWEN_EDIT_STYLE_FALLBACK
        else:
            style = re.sub(r"\s+", " ", raw).strip(" \t\r\n\"'`")
        if not style:
            style = QWEN_EDIT_STYLE_FALLBACK
        style = re.sub(r"\s*,\s*,+", ", ", style).strip(" ,")
        if not style:
            style = QWEN_EDIT_STYLE_FALLBACK
        guards = (
            "style tag only, do not render words, no visible text, no watermark, "
            "no signature, no logo, no visible artist name"
        )
        style_lower = style.lower()
        if not any(marker in style_lower for marker in ("no text", "no visible text", "do not render words")):
            style = f"{style}, {guards}"
        return style[:700]

    def _prompt_has_cjk(prompt):
        return bool(re.search(r"[\u3400-\u9fff]", str(prompt or "")))

    def _english_background_target_from_prompt(text, lower):
        if "屋頂" in text or "rooftop" in lower:
            if "夜" in text or "night" in lower:
                return "a nighttime city rooftop with distant lights"
            if "黃昏" in text or "夕陽" in text or "sunset" in lower or "dusk" in lower:
                return "a sunset city rooftop with warm sky and distant buildings"
            return "a city rooftop with distant buildings"
        if "城市" in text or "city" in lower:
            if "夜" in text or "night" in lower:
                return "a nighttime city background with distant lights"
            if "黃昏" in text or "夕陽" in text or "sunset" in lower or "dusk" in lower:
                return "a sunset city background with warm sky"
            return "a clean city background"
        if "花園" in text or "garden" in lower:
            return "a bright flower garden background"
        if "海邊" in text or "beach" in lower:
            return "a sunny beach background"
        if "教室" in text or "classroom" in lower:
            return "a clean anime classroom background"
        if re.search(r"[\u3400-\u9fff]", text):
            return "the requested scenic anime background"
        return text or "the requested new background"

    def _derive_qwen_edit_instruction_from_prompt(prompt):
        text = re.sub(r"\s+", " ", str(prompt or "")).strip()
        if not text or not _prompt_has_cjk(text):
            return ""
        lower = text.lower()
        preserve_identity = "preserve face, expression, hairstyle, hands, pose, body, and background"
        wants_remove_hairclips = (
            ("移除" in text or "刪除" in text or "拿掉" in text or "remove" in lower or "delete" in lower)
            and ("髮夾" in text or "hair clip" in lower or "hairclip" in lower)
        )
        wants_scarf = "圍巾" in text or "scarf" in lower
        wants_yandere = "病嬌" in text or "yandere" in lower
        wants_lace = "蕾絲" in text or "lace" in lower or "lacy" in lower
        wants_cat_ears = "貓耳" in text or "cat-ear" in lower or "cat ear" in lower or "cat ears" in lower
        wants_index_finger_lips = (
            ("食指" in text or "index finger" in lower)
            and ("嘴唇" in text or "嘴邊" in text or "唇" in text or "lips" in lower or "mouth" in lower)
        )
        wants_left_hand_behind_back = (
            ("左手" in text or "left hand" in lower or "left arm" in lower)
            and ("背後" in text or "behind back" in lower or "behind her back" in lower)
        )
        wants_head_tilt = "頭歪" in text or "歪著" in text or "tilted head" in lower or "head tilted" in lower
        wants_twin_tails = (
            "雙馬尾" in text
            or "twin tails" in lower
        ) and bool(re.search(
            r"(改成|改為|換成|變成|加入|新增|加上|只把|change|turn|make|add)",
            text,
            re.IGNORECASE,
        ))
        wants_larger_bust = bool(re.search(
            r"(胸部|胸口|胸圍|bust|chest)[^。．；;\n\r]{0,20}(變大|更大|放大|較大|larger|bigger)",
            text,
            re.IGNORECASE,
        )) or bool(re.search(
            r"(變大|更大|放大|較大|larger|bigger)[^。．；;\n\r]{0,20}(胸部|胸口|胸圍|bust|chest)",
            text,
            re.IGNORECASE,
        ))
        wants_cross_reference_blend = (
            bool(re.search(r"chara\s+reference|character\s+reference|角色.*參考|角色外觀", text, re.IGNORECASE))
            and bool(re.search(r"clothes\s+reference|clothing\s+reference|outfit\s+reference|服裝.*參考", text, re.IGNORECASE))
            and bool(re.search(r"pose\s+reference|姿勢.*參考|動作.*參考", text, re.IGNORECASE))
        ) or bool(re.search(r"交叉.*參考|三張.*reference|多參考圖", text, re.IGNORECASE))
        if wants_cross_reference_blend:
            return (
                "use the character reference only for the main character appearance, face mood, hairstyle direction, and color cues; "
                "use the clothes reference only for outfit design, garment shape, colors, and details; "
                "use the pose reference only for body pose, limb placement, and composition; "
                "apply those three references to the source character as one coherent anime girl while preserving the source image as the base; "
                "do not copy reference backgrounds, do not copy unrelated identities, do not mix up clothes and pose roles, and do not add text or watermark; "
                "avoid extra limbs, broken hands, missing fingers, body penetration, or black/gray failure output."
            )
        if wants_remove_hairclips and wants_scarf and wants_yandere and wants_lace and wants_larger_bust:
            hairstyle_clause = (
                "change the hairstyle to clear high twin tails while keeping the same dark blue hair color; "
                if wants_twin_tails else
                "preserve the main hair length and dark blue hair color; "
            )
            cat_ears_clause = (
                "add clear cat-ear hair accessories on top of the head, separate from the removed white hair clips; "
                if wants_cat_ears else
                ""
            )
            pose_clause = (
                "change the pose so the right index finger gently touches the lips, the left hand reaches behind the back, "
                "and the head is tilted; keep the gesture anatomically plausible with visible natural fingers; "
                if wants_index_finger_lips and wants_left_hand_behind_back and wants_head_tilt else
                "preserve the pose and hands; "
            )
            return (
                "remove the white hair clips and fill those areas with natural dark blue hair; "
                f"{hairstyle_clause}"
                f"{cat_ears_clause}"
                f"{pose_clause}"
                "add a clearly visible soft dark red scarf around the neck without covering the whole face; "
                "change the facial expression to yandere with intense eyes and a slightly dangerous smile, no horror and no gore; "
                "make the bust moderately larger while keeping natural anatomy, the same identity, and believable clothing tension; "
                "change the visible white dress into a delicate white lace dress with lace fabric texture, lace trim, and subtle frills; "
                "preserve the same girl, background, composition, and overall anime style; "
                "do not add text, watermark, extra people, or unrelated objects."
            )
        wants_vertical_full_body_festival = (
            ("1080x1920" in lower or "1080×1920" in text)
            and ("全身" in text or "full body" in lower)
            and ("大街" in text or "街" in text or "street" in lower)
            and ("車水馬龍" in text or "traffic" in lower or "busy street" in lower)
            and ("和服" in text or "kimono" in lower)
            and ("木屐" in text or "geta" in lower)
            and ("單馬尾" in text or "ponytail" in lower)
        )
        if wants_vertical_full_body_festival:
            return (
                "convert the image into a vertical 1080x1920 full-body composition showing the same girl from head to feet; "
                "make sure both feet are visible and she is wearing traditional wooden geta sandals; "
                "change the visible outfit to a Japanese festival kimono with sleeves, collar, obi sash, and tasteful festival fabric details; "
                "change the hairstyle to a single ponytail with Japanese festival hair accessories; "
                "change the background to a busy city street during a Japanese festival with traffic, street lights, and blurred pedestrians; "
                "keep pedestrians softly blurred and secondary, preserve the same face identity and overall anime style; "
                "avoid cropped feet, missing legs, extra limbs, text, watermark, and extra main characters."
            )
        wants_body_lace_proportion_test = (
            ("體態" in text or "身形" in text or "body proportion" in lower or "body proportions" in lower)
            and ("高挑" in text or "更高" in text or "taller" in lower)
            and ("腰" in text or "waist" in lower)
            and wants_larger_bust
            and ("腿" in text or "legs" in lower)
            and ("蕾絲" in text or "lace" in lower)
        )
        if wants_body_lace_proportion_test:
            return (
                "edit the same girl into a full-body standing adult anime woman with a taller, more elegant silhouette; "
                "make the waist visibly slimmer, make the legs longer and more graceful, and make the bust moderately larger while keeping natural anatomy and believable clothing tension; "
                "change the outfit to a fully lined opaque white lace maxi dress: solid white fabric underneath, lace as decorative overlay only, lace trim, subtle frills, a clear real skirt hem, and modest coverage; "
                "skin must not be visible through the dress on the torso, hips, thighs, or legs; "
                "preserve the original footwear if possible, otherwise add simple white dress shoes or geta sandals with both feet visible; "
                "do not make it a bodysuit, swimsuit, transparent outfit, lingerie, qipao, cheongsam, visible underwear, or bare-feet fashion pose; "
                "preserve the same face identity, dark blue hair color, single ponytail, festival hair accessories, anime style, and the busy night street background; "
                "keep both feet visible inside the frame; avoid extra fingers, missing fingers, broken hands, impossible body proportions, body or clothing penetration, cropped feet, text, watermark, and extra people."
            )
        if "新增第二位" in text or "第二位清楚" in text or "second" in lower:
            return (
                "create a new full separate second anime girl friend occupying the viewer-right third of the image, standing slightly behind the original girl; "
                "make enough visible space for the new character and slightly shift or scale the original girl only if needed, instead of ignoring the added person; "
                "the second girl must be visibly present from head to at least upper body, not just a shadow or background pedestrian; "
                "make the interaction clear: the second girl gently places one hand on the original girl's shoulder, both girls look toward the camera and smile; "
                "make the new girl match the original scene and costume context, especially if the original scene uses a festival kimono/yukata or other traditional outfit; "
                "give the new girl a coordinated festival yukata/kimono and compatible accessories instead of modern casual clothes unless the user explicitly asks for contrast; "
                "preserve the original girl identity, face, hairstyle, clothing, hands, pose, lighting, background, and overall scene as much as possible; "
                "do not replace the original girl, do not merge bodies or faces, do not create two heads on one body, and do not let the interaction hand penetrate the shoulder, body, or clothing or cover either face."
            )
        if "水手服" in text or "sailor collar" in lower:
            return (
                "change only the visible outfit to a Japanese sailor uniform with a navy sailor collar, "
                "white blouse, and red ribbon; preserve face, expression, hairstyle, hair accessories, hands, pose, body, and background."
            )
        if "紅色連帽" in text or "red hoodie" in lower:
            return (
                "change only the visible outfit to a red hoodie with red sleeves and small white drawstrings; "
                f"{preserve_identity}."
            )
        if "和服" in text or "kimono" in lower:
            return (
                "change only the visible outfit to a pale Japanese kimono with a clear collar, sleeves, and obi sash; "
                f"{preserve_identity}."
            )
        if "bikini" in lower:
            return (
                "change only the visible outfit to a tasteful two-piece bikini with visible shoulder straps; "
                f"{preserve_identity}."
            )
        if "泳裝" in text or "泳衣" in text or "swimsuit" in lower:
            return (
                "change only the visible outfit to a modest one-piece swimsuit; "
                f"{preserve_identity}."
            )
        if "小惡魔" in text or "little devil" in lower:
            return (
                "change only the visible outfit to a cute little-devil cosplay costume with dark dress, red ribbon accents, "
                "and small devil-horn hair accessories; preserve face, expression, main hairstyle, hands, pose, body, and background."
            )
        wants_open_arms = (
            "張開雙臂" in text
            or "雙臂張開" in text
            or bool(re.search(r"(?:兩隻|雙|二隻|2\s*隻)?\s*手臂[^。．；;\n\r]{0,20}(?:左右|兩側|向外|外側)?[^。．；;\n\r]{0,10}張開", text))
            or bool(re.search(r"張開[^。．；;\n\r]{0,12}(?:兩隻|雙|二隻|2\s*隻)?\s*手臂", text))
            or "open arms" in lower
            or "arms open" in lower
            or "spread arms" in lower
        )
        wants_bed_scene = (
            "床" in text
            or "躺" in text
            or "bed" in lower
            or "lying" in lower
            or "laying" in lower
        )
        wants_outpaint_or_wide = (
            "outpaint" in lower
            or "外延" in text
            or "擴圖" in text
            or "1920x1080" in lower
            or "1920×1080" in text
            or "橫幅" in text
        )
        if wants_open_arms and wants_bed_scene and wants_outpaint_or_wide:
            return (
                "convert the image into a 16:9 wide composition as if outpainted left and right; "
                "place the same girl lying on a bed with pillows and soft bedding in the background; "
                "both arms are opened outward to the sides with both hands fully visible inside the frame; "
                "extrapolate only the previously covered front of the outfit from the existing design; "
                "preserve all unrequested original clothing attributes, including garment wearing state, exposure, neckline height, "
                "fabric coverage, straps, accessories, folds, colors, and how each layer is draped on the body; "
                "if the original shoulders or collarbones are visible, keep the same visible skin areas; "
                "if the original cardigan or outer layer is slipped below the shoulders, keep it slipped below the shoulders "
                "and draped on the upper arms or forearms instead of pulled back onto the shoulders; preserve the same red ribbon shape "
                "and position, same shoulder strap positions, same beige cardigan edges, colors, and clothing style; "
                "naturally complete the white dress, cardigan interior, clothing folds, and body contours; "
                "do not add new fabric coverage, do not change how garments are worn, "
                "do not redesign the outfit, do not change the face, hair, hair clips, ribbon, straps, cardigan cut, or color palette; "
                "avoid extra arms, broken hands, missing fingers, cropped hands, text, watermark, and extra people."
            )
        if "背景" in text and re.search(r"(?:把|將)?背景\s*(?:改成|改為|換成|替換成|變成|改變為)", text):
            match = re.search(r"(?:把|將)?背景\s*(?:改成|改為|換成|替換成|變成|改變為)\s*([^；;。．\n\r]+)", text)
            target = re.sub(r"\s+", " ", match.group(1)).strip(" ，,。．;；") if match else "the requested new background"
            target = _english_background_target_from_prompt(target, target.lower())
            return f"change only the background to {target}; preserve the girl, face, hair, outfit, pose, body, and foreground objects."
        explicit_pose_edit = bool(re.search(r"(?:把|將)?(?:女孩|人物|角色)?(?:的)?(?:姿勢|動作)\s*(?:改成|改為|換成|變成|改變為)", text))
        if explicit_pose_edit or "pose to" in lower:
            if "敬禮" in text or "salute" in lower:
                pose = "a casual salute pose with one hand raised beside the forehead"
            elif "揮手" in text or "waving" in lower or "wave" in lower:
                pose = "a clear waving-hand pose"
            elif "v sign" in lower or "v字" in lower or "勝利" in text:
                pose = "a V-sign peace pose"
            elif wants_open_arms:
                return (
                    "change the girl's pose so both arms are opened outward to the sides, away from the chest; "
                    "extrapolate only the previously covered area from the existing outfit; preserve all unrequested original clothing "
                    "attributes, including garment wearing state, exposure, neckline height, fabric coverage, straps, accessories, folds, "
                    "colors, and how each layer is draped on the body; if the original shoulders or collarbones are visible, keep the same "
                    "visible skin areas; if the original cardigan or outer layer is slipped below the shoulders, keep it slipped below the "
                    "shoulders and draped on the upper arms or forearms instead of pulled back onto the shoulders; naturally complete the white dress, "
                    "same neckline height, same red ribbon shape and position, same shoulder strap positions, same beige cardigan edges, "
                    "cardigan interior, clothing folds, and body contours; do not redesign the outfit, do not change the neckline, "
                    "ribbon, straps, cardigan cut, colors, or background; do not add new fabric coverage or change how garments are worn; "
                    "preserve identity, face, hair, body proportions, and style; "
                    "avoid extra arms, broken hands, missing fingers, or cropped hands."
                )
            elif "cross" in lower or "交叉" in text:
                pose = "an arms-crossed pose"
            else:
                pose = "the requested new pose"
            return f"change the girl's pose to {pose}; preserve identity, face, hair, outfit, body proportions, and background."
        if "真實" in text or "realistic" in lower or "寫實" in text:
            return (
                "convert the image to a more realistic semi-realistic illustration style while preserving the same girl, "
                "face, hair, outfit, pose, composition, and background."
            )
        if "貓耳" in text or "cat-ear" in lower or "cat ear" in lower:
            return f"add cat-ear hair accessories to the girl; {preserve_identity}."
        if "換臉" in text or "臉換" in text or "change face" in lower or "face identity" in lower:
            return (
                "change only the girl's face identity to a different anime character face with a slightly more mature face shape "
                "and different eye shape; preserve hairstyle, hair color, outfit, hands, pose, body, composition, and background."
            )
        if wants_yandere:
            return (
                "change only the facial expression to yandere with intense eyes and a slightly dangerous smile, no horror and no gore; "
                "preserve hair, outfit, hands, pose, body, and background."
            )
        if wants_twin_tails:
            return f"change only the hairstyle to high twin tails; preserve face, expression, outfit, hands, pose, body, and background."
        if "髮色" in text or "銀" in text or "silver" in lower:
            return f"change only the hair color to silver-white; preserve face, expression, outfit, hands, pose, body, and background."
        if "表情" in text or "驚訝" in text or "surprised" in lower:
            return f"change only the facial expression to surprised with a slightly open mouth; preserve hair, outfit, hands, pose, body, and background."
        if "手環" in text or "wristband" in lower:
            return f"remove only the black wristband from the girl's wrist; {preserve_identity}."
        if "項鍊" in text or "necklace" in lower:
            return (
                "replace only the visible neck ribbon with a simple small gold necklace; "
                f"{preserve_identity}."
            )
        if (
            ("第二張" in text or "參考圖" in text or "reference" in lower)
            and ("姿勢" in text or "pose" in lower)
            and ("床" in text or "bed" in lower)
            and ("睡衣" in text or "pajama" in lower or "pyjama" in lower)
        ):
            return (
                "use the reference image only for the pose; change the source character to match the reference pose; "
                "change the scene to a bedroom on a bed; change the outfit to pajamas; preserve the source character identity, "
                "face, hairstyle, hair color, body proportions, and anime style; do not copy the reference character identity, "
                "face, hair, ears, tail, bath scene, towel, or background."
            )
        if re.search(r"(改成|改為|換成|替換|移除|刪除|修正|修復|新增)", text):
            return (
                "apply only the requested semantic image edit from the user instruction; preserve all unmentioned subjects, "
                "identity, face, hair, outfit, pose, body, composition, and background as much as possible."
            )
        return ""

    def _is_qwen_edit_workflow_id(workflow_id):
        workflow_id = str(workflow_id or "").strip()
        return workflow_id == "origin_qwen_image_edit_2509" or workflow_id.startswith("origin_qwen_image_edit_2509_")

    def _qwen_edit_instruction_needs_derived_override(current_instruction, derived_instruction):
        current = str(current_instruction or "").strip().lower()
        derived = str(derived_instruction or "").strip().lower()
        if not current or not derived:
            return False
        derived_is_cross_reference = (
            "character reference" in derived
            and "clothes reference" in derived
            and "pose reference" in derived
        )
        if derived_is_cross_reference:
            current_is_cross_reference = (
                "character reference" in current
                and ("clothes reference" in current or "outfit reference" in current)
                and "pose reference" in current
            )
            if current_is_cross_reference:
                return False
            return bool(re.search(r"\b(change\s+only|hair\s+color|silver|silver-white)\b|髮色|銀髮|銀白", current, re.IGNORECASE))
        strict_preservation_terms = (
            "garment wearing state",
            "exposure",
            "fabric coverage",
            "how each layer is draped",
            "visible skin areas",
            "slipped below the shoulders",
            "do not add new fabric coverage",
            "change how garments are worn",
        )
        derived_requires_strict_preservation = any(term in derived for term in strict_preservation_terms)
        if not derived_requires_strict_preservation:
            return False
        current_has_strict_preservation_guard = any(term in current for term in strict_preservation_terms)
        if current_has_strict_preservation_guard:
            return False
        pose_or_canvas_terms = (
            "open",
            "spread",
            "arms",
            "bed",
            "lying",
            "outpaint",
            "16:9",
            "1920",
            "wide",
        )
        return any(term in current for term in pose_or_canvas_terms)

    def _normalize_qwen_edit_inline_instruction(normalized):
        workflow_id = str(
            normalized.get("official_workflow_id")
            or normalized.get("workflow_id")
            or normalized.get("template_id")
            or ""
        ).strip()
        prompt = str(normalized.get("prompt") or "").strip()
        is_qwen_edit = _is_qwen_edit_workflow_id(workflow_id) or str(normalized.get("generation_mode") or "").strip().lower() == "img2img"
        instruction = _extract_inline_edit_instruction(prompt)
        if not instruction and is_qwen_edit:
            instruction = _derive_qwen_edit_instruction_from_prompt(prompt)
        if not instruction:
            return
        current_instruction = str(
            normalized.get("edit_instruction")
            or normalized.get("edit_prompt")
            or ""
        ).strip()
        if (
            not current_instruction
            or current_instruction == prompt
            or _extract_inline_edit_instruction(current_instruction)
            or _qwen_edit_instruction_needs_derived_override(current_instruction, instruction)
        ):
            normalized["edit_instruction"] = instruction
        if is_qwen_edit:
            normalized["prompt"] = _sanitize_qwen_edit_style_context(
                _extract_prompt_style_context(prompt) or "anime style, 1girl"
            )

    def _official_workflow_id_from_body(body):
        value = str((body or {}).get("official_workflow_id") or (body or {}).get("workflow_id") or (body or {}).get("template_id") or "").strip()
        return value

    def _should_run_official_workflow(body):
        workflow_id = _official_workflow_id_from_body(body)
        return workflow_id and workflow_id not in AI_AGENT_COMFYUI_LEGACY_SHORTCUT_WORKFLOWS

    def _build_write_tool_request(tool_name, spec, args):
        args = dict(args or {})
        if tool_name == "write_chat_create_room":
            if _is_missing_arg(args.get("join_password")) and not _is_missing_arg(args.get("password")):
                args["join_password"] = args.get("password")
            if _is_missing_arg(args.get("target_user")) and not _is_missing_arg(args.get("target_username")):
                args["target_user"] = args.get("target_username")
        if tool_name == "write_chat_send_message" and _is_missing_arg(args.get("content")) and not _is_missing_arg(args.get("message")):
            args["content"] = args.get("message")
        if tool_name == "write_chat_friend_request" and _is_missing_arg(args.get("username")):
            for alias in ("target_username", "target", "friend_username"):
                if not _is_missing_arg(args.get(alias)):
                    args["username"] = args.get(alias)
                    break
        if tool_name == "write_notification_send":
            if _is_missing_arg(args.get("body")):
                for alias in ("message", "content"):
                    if not _is_missing_arg(args.get(alias)):
                        args["body"] = args.get(alias)
                        break
            if _is_missing_arg(args.get("user_id")) and not _is_missing_arg(args.get("target_user_id")):
                args["user_id"] = args.get("target_user_id")
        if tool_name == "write_appeal_review":
            if _is_missing_arg(args.get("action")) and not _is_missing_arg(args.get("decision")):
                args["action"] = args.get("decision")
            if _is_missing_arg(args.get("note")) and not _is_missing_arg(args.get("review_note")):
                args["note"] = args.get("review_note")
        if tool_name == "write_appeal_create" and _is_missing_arg(args.get("content")) and not _is_missing_arg(args.get("message")):
            args["content"] = args.get("message")
        if tool_name == "write_storage_quota_override" and _is_missing_arg(args.get("quota_bytes")) and not _is_missing_arg(args.get("quota_mb")):
            try:
                args["quota_bytes"] = int(float(args.get("quota_mb")) * 1024 * 1024)
            except Exception:
                pass
        if tool_name == "write_cloud_drive_text_update" and _is_missing_arg(args.get("content")) and not _is_missing_arg(args.get("text")):
            args["content"] = args.get("text")
        if tool_name in {"write_comfyui_civitai_inspect", "write_comfyui_civitai_download"}:
            if _is_missing_arg(args.get("url")) and not _is_missing_arg(args.get("model_url")):
                args["url"] = args.get("model_url")
        if tool_name in {"write_cloud_drive_remote_download", "write_remote_download_direct", "write_remote_download_bt"}:
            for alias in ("url", "download_url", "source_url", "magnet_uri", "magnet", "torrent_url"):
                value = str(args.get(alias) or "").strip()
                if value:
                    args["url"] = value
                    break
        if tool_name == "write_cloud_drive_remote_download" and not str(args.get("source_type") or "").strip():
            url_value = str(args.get("url") or "").strip().lower()
            args["source_type"] = "bt" if url_value.startswith("magnet:") or url_value.endswith(".torrent") else "direct"
        if tool_name == "write_album_add_file":
            cloud_file_id = str(args.get("cloud_file_id") or "").strip()
            if cloud_file_id and not str(args.get("file_id") or args.get("storage_file_id") or "").strip():
                args["file_id"] = cloud_file_id
        if tool_name == "write_comfyui_generate":
            args = _normalize_comfyui_write_args(args)
            if _is_missing_arg(args.get("edit_instruction")) and not _is_missing_arg(args.get("edit_prompt")):
                args["edit_instruction"] = args.get("edit_prompt")
        if tool_name == "write_comfyui_background_composite":
            if _is_missing_arg(args.get("source_image_ref")) and not _is_missing_arg(args.get("source_image_ref_json")):
                args["source_image_ref"] = args.get("source_image_ref_json")
            if _is_missing_arg(args.get("background_image_ref")) and not _is_missing_arg(args.get("reference_image_ref")):
                args["background_image_ref"] = args.get("reference_image_ref")
            if _is_missing_arg(args.get("background_image_ref_json")) and not _is_missing_arg(args.get("reference_image_ref_json")):
                args["background_image_ref_json"] = args.get("reference_image_ref_json")
            if _is_missing_arg(args.get("background_image_ref")) and not _is_missing_arg(args.get("background_image_ref_json")):
                args["background_image_ref"] = args.get("background_image_ref_json")
        missing = [
            key for key in sorted(spec.get("required") or [])
            if _is_missing_arg(args.get(key))
        ]
        if missing:
            return None, None, f"缺少必要參數：{', '.join(missing)}"
        if tool_name == "write_album_add_file" and not str(args.get("file_id") or args.get("storage_file_id") or "").strip():
            return None, None, "缺少必要參數：file_id 或 storage_file_id"

        path = spec.get("path") or ""
        for name, kind in (spec.get("path_params") or {}).items():
            value, msg = _coerce_write_path_param(name, args.get(name), kind)
            if msg:
                return None, None, msg
            path = path.replace("{" + name + "}", str(value))

        query = {}
        for key in spec.get("query_fields") or set():
            if key not in args:
                continue
            value = args.get(key)
            if tool_name == "write_launch_doc_read" and key == "path":
                value, msg = _validate_launch_doc_path(value)
                if msg:
                    return None, None, msg
            query[key] = value
        if query:
            path = f"{path}?{urlencode(query)}"

        body_fields = spec.get("body_fields") or set()
        body = {key: args.get(key) for key in body_fields if key in args}
        if tool_name == "write_community_create_thread" and "post_type" in body:
            post_type = str(body.get("post_type") or "").strip().lower()
            post_type_aliases = {
                "": "normal",
                "discussion": "normal",
                "general": "normal",
                "post": "normal",
                "thread": "normal",
                "討論": "normal",
                "一般": "normal",
                "普通": "normal",
                "guide": "howto",
                "教學": "howto",
                "問題": "question",
                "提問": "question",
            }
            body["post_type"] = post_type_aliases.get(post_type, post_type)
        if tool_name == "write_comfyui_generate" and not str(body.get("model") or "").strip():
            fallback_model = str(body.get("checkpoint") or body.get("checkpoint_name") or "").strip()
            if fallback_model:
                body["model"] = fallback_model
        if tool_name == "write_comfyui_generate":
            body = {key: value for key, value in body.items() if not _is_missing_arg(value)}
        if tool_name == "write_remote_download_direct":
            body["source_type"] = "direct"
            body["download_mode"] = "direct"
        if tool_name == "write_remote_download_bt":
            body["download_mode"] = "bt"
        if tool_name == "write_cloud_drive_remote_download" and not str(body.get("source_type") or "").strip():
            body["source_type"] = "direct"
        if tool_name == "write_cloud_drive_remote_download" and not str(body.get("download_mode") or "").strip():
            body["download_mode"] = "bt" if str(body.get("source_type") or "").strip().lower() in {"bt", "magnet", "torrent_url", "torrent_file"} else "direct"
        return path, body, ""

    def _prepare_comfyui_write_body(body):
        next_body = dict(body or {})
        next_body, canvas_msg = _prepare_qwen_edit_canvas_source_if_needed(next_body)
        if canvas_msg:
            return None, canvas_msg
        requested = str(
            next_body.get("model")
            or next_body.get("checkpoint")
            or next_body.get("checkpoint_name")
            or ""
        ).strip()
        if _should_run_official_workflow(next_body):
            if not requested:
                return next_body, ""
            status_code, models_payload = _dispatch_internal_api("GET", "/api/comfyui/models", None)
            model_options = []
            if 200 <= int(status_code or 500) < 400 and isinstance(models_payload, dict):
                model_options = list(models_payload.get("models") or [])
            if not model_options:
                msg = ""
                if isinstance(models_payload, dict):
                    msg = str(models_payload.get("msg") or "").strip()
                suffix = f"：{msg}" if msg else ""
                return None, f"目前無法讀取 ComfyUI checkpoint 清單，已取消送出產圖{suffix}"
            resolved, msg, _matches = _resolve_comfyui_checkpoint_name(requested, model_options)
            if msg:
                return None, msg
            if resolved:
                next_body["model"] = resolved
                next_body["checkpoint"] = resolved
                next_body["checkpoint_name"] = resolved
            return next_body, ""
        status_code, models_payload = _dispatch_internal_api("GET", "/api/comfyui/models", None)
        model_options = []
        if 200 <= int(status_code or 500) < 400 and isinstance(models_payload, dict):
            model_options = list(models_payload.get("models") or [])
        if not model_options:
            msg = ""
            if isinstance(models_payload, dict):
                msg = str(models_payload.get("msg") or "").strip()
            suffix = f"：{msg}" if msg else ""
            return None, f"目前無法讀取 ComfyUI checkpoint 清單，已取消送出產圖{suffix}"
        if not requested:
            resolved = _preferred_comfyui_checkpoint_option(model_options)
            if resolved:
                next_body["model"] = resolved
                next_body["checkpoint"] = resolved
                next_body["checkpoint_name"] = resolved
            return next_body, ""
        resolved, msg, _matches = _resolve_comfyui_checkpoint_name(requested, model_options)
        if msg:
            return None, msg
        if resolved:
            next_body["model"] = resolved
            next_body["checkpoint"] = resolved
            next_body["checkpoint_name"] = resolved
        return next_body, ""

    def _dispatch_internal_api(method, path, body):
        headers = {}
        csrf = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken") or request.cookies.get("csrf_token") or ""
        if csrf:
            headers["X-CSRF-Token"] = csrf
        user_agent = request.headers.get("User-Agent") or ""
        if user_agent:
            headers["User-Agent"] = user_agent
        with app.test_client() as client:
            for name, value in request.cookies.items():
                client.set_cookie(str(name), str(value))
            response = client.open(
                path,
                method=method,
                json=body if method in {"POST", "PUT", "PATCH", "DELETE"} else None,
                headers=headers,
                environ_base={"hackme.internal_dispatch": "ai_agent_write_tool"},
            )
        payload = response.get_json(silent=True)
        if payload is None:
            payload = {"raw": response.get_data(as_text=True)[:4000]}
        return response.status_code, payload

    def _dispatch_internal_image_upload(filename, data, mime_type="image/png", *, backend_url=""):
        headers = {}
        csrf = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken") or request.cookies.get("csrf_token") or ""
        if csrf:
            headers["X-CSRF-Token"] = csrf
        user_agent = request.headers.get("User-Agent") or ""
        if user_agent:
            headers["User-Agent"] = user_agent
        with app.test_client() as client:
            for name, value in request.cookies.items():
                client.set_cookie(str(name), str(value))
            form_data = {"image": (io.BytesIO(data), filename, mime_type)}
            if backend_url:
                form_data["backend_url"] = backend_url
                form_data["comfyui_backend_url"] = backend_url
            response = client.open(
                "/api/comfyui/import-uploaded-image",
                method="POST",
                data=form_data,
                content_type="multipart/form-data",
                headers=headers,
                environ_base={"hackme.internal_dispatch": "ai_agent_write_tool"},
            )
        payload = response.get_json(silent=True)
        if payload is None:
            payload = {"raw": response.get_data(as_text=True)[:4000]}
        return response.status_code, payload

    def _workflow_node_input_patch(user_inputs, node_id, input_name, value):
        if _is_missing_arg(value):
            return
        node_key = str(node_id or "").strip()
        input_key = str(input_name or "").strip()
        if not node_key or not input_key:
            return
        patch = user_inputs.setdefault(node_key, {})
        patch[input_key] = value

    def _workflow_field_node_id(field):
        raw = str((field or {}).get("id") or "").strip()
        match = re.match(r"^node:([^:]+):", raw)
        return match.group(1) if match else ""

    def _workflow_fields_from_preset(preset):
        manifest = preset.get("manifest_json") if isinstance(preset, dict) else {}
        panels = ((manifest or {}).get("ui") or {}).get("panels") if isinstance(manifest, dict) else []
        fields = []
        for panel in panels or []:
            if not isinstance(panel, dict):
                continue
            for field in panel.get("fields") or []:
                if isinstance(field, dict):
                    fields.append(field)
        return fields

    def _source_image_filename(body):
        ref = (body or {}).get("source_image_ref") or (body or {}).get("source_image_ref_json")
        if isinstance(ref, str):
            try:
                ref = json.loads(ref)
            except Exception:
                ref = {"filename": ref}
        if isinstance(ref, dict):
            return str(ref.get("filename") or "").strip()
        return ""

    def _image_ref_from_body(body, key):
        ref = (body or {}).get(key) or (body or {}).get(f"{key}_json")
        if isinstance(ref, str):
            try:
                ref = json.loads(ref)
            except Exception:
                ref = {"filename": ref}
        return ref if isinstance(ref, dict) else {}

    def _cloud_file_id_from_image_ref(ref):
        if not isinstance(ref, dict):
            return ""
        for key in ("cloud_file_id", "file_id", "uploaded_file_id"):
            value = str(ref.get(key) or "").strip()
            if value:
                return value
        return ""

    def _save_comfyui_ref_to_cloud_file_id(ref):
        if not isinstance(ref, dict) or not str(ref.get("filename") or "").strip():
            return "", None
        status_code, payload = _dispatch_internal_api("POST", "/api/comfyui/save", {"image_ref": ref})
        if not (200 <= int(status_code or 500) < 400) or not isinstance(payload, dict) or not payload.get("ok"):
            return "", payload if isinstance(payload, dict) else {"ok": False, "msg": f"HTTP {status_code}"}
        file_info = payload.get("file") if isinstance(payload.get("file"), dict) else {}
        return str(file_info.get("file_id") or "").strip(), payload

    def _resolve_cloud_file_id_for_image_ref(ref):
        cloud_file_id = _cloud_file_id_from_image_ref(ref)
        if cloud_file_id:
            return cloud_file_id, None
        return _save_comfyui_ref_to_cloud_file_id(ref)

    def _target_canvas_dimensions_from_body(body):
        try:
            width = int(float((body or {}).get("width") or 0))
            height = int(float((body or {}).get("height") or 0))
        except Exception:
            return 0, 0
        if width < 64 or height < 64 or width > 2048 or height > 2048:
            return 0, 0
        return width, height

    def _resolve_uploaded_file_path_for_actor(file_id):
        actor = get_current_user_ctx() or {}
        actor_id = int(_actor_value(actor, "id", 0) or 0)
        if actor_id <= 0:
            return None, "尚未登入，無法讀取來源圖片"
        conn = get_db()
        try:
            row = conn.execute(
                """
                SELECT id, owner_user_id, storage_path, original_filename_plain_for_public
                FROM uploaded_files
                WHERE id=? AND owner_user_id=? AND deleted_at IS NULL
                """,
                (str(file_id or "").strip(), actor_id),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None, "找不到可用的來源圖片檔案"
        raw_path = str(row["storage_path"] or "").strip()
        if not raw_path:
            return None, "來源圖片缺少儲存路徑"
        path = os.path.abspath(raw_path) if os.path.isabs(raw_path) else ""
        candidates = []
        if path:
            candidates.append(path)
        else:
            try:
                from services.storage.paths import resolve_storage_path
                candidates.append(str(resolve_storage_path(storage_root, raw_path, create_parent=False)))
            except Exception:
                candidates.append(os.path.join(str(storage_root or "."), raw_path))
        for candidate in candidates:
            if candidate and os.path.isfile(candidate):
                return candidate, ""
        return None, f"來源圖片實體不存在：{raw_path}"

    def _qwen_edit_source_canvas_needed(body, image_size):
        workflow_id = _official_workflow_id_from_body(body)
        if not _is_qwen_edit_workflow_id(workflow_id):
            return False
        mode = str((body or {}).get("generation_mode") or "").strip().lower()
        if mode and mode != "img2img":
            return False
        width, height = _target_canvas_dimensions_from_body(body)
        if not width or not height:
            return False
        source_width, source_height = image_size
        if source_width <= 0 or source_height <= 0:
            return False
        source_ratio = source_width / source_height
        target_ratio = width / height
        return abs(source_ratio - target_ratio) > 0.03

    def _render_qwen_edit_canvas(source_path, *, width, height):
        try:
            from PIL import Image, ImageFilter
        except Exception as exc:
            return None, (0, 0), f"Pillow 載入失敗，無法建立 Qwen Edit 寬畫布：{exc}"
        try:
            with Image.open(source_path) as img:
                source = img.convert("RGB")
        except Exception as exc:
            return None, (0, 0), f"來源圖片讀取失敗：{exc}"
        source_size = source.size
        src_w, src_h = source_size
        if src_w <= 0 or src_h <= 0:
            return None, source_size, "來源圖片尺寸不合法"

        cover_scale = max(width / src_w, height / src_h)
        cover_size = (max(1, int(round(src_w * cover_scale))), max(1, int(round(src_h * cover_scale))))
        cover = source.resize(cover_size, Image.Resampling.LANCZOS)
        left = max(0, (cover.width - width) // 2)
        top = max(0, (cover.height - height) // 2)
        canvas = cover.crop((left, top, left + width, top + height)).filter(ImageFilter.GaussianBlur(radius=24))

        fit_scale = min(width / src_w, height / src_h)
        fit_size = (max(1, int(round(src_w * fit_scale))), max(1, int(round(src_h * fit_scale))))
        foreground = source.resize(fit_size, Image.Resampling.LANCZOS)
        paste_left = (width - foreground.width) // 2
        paste_top = (height - foreground.height) // 2
        canvas.paste(foreground, (paste_left, paste_top))
        out = io.BytesIO()
        canvas.save(out, format="PNG", optimize=True)
        return out.getvalue(), source_size, ""

    def _prepare_qwen_edit_canvas_source_if_needed(body):
        next_body = dict(body or {})
        source_ref = _image_ref_from_body(next_body, "source_image_ref")
        if not source_ref:
            return next_body, ""
        width, height = _target_canvas_dimensions_from_body(next_body)
        if not width or not height:
            return next_body, ""
        source_cloud, save_payload = _resolve_cloud_file_id_for_image_ref(source_ref)
        if not source_cloud:
            msg = ""
            if isinstance(save_payload, dict):
                msg = str(save_payload.get("msg") or "").strip()
            audit(
                "AI_AGENT_QWEN_EDIT_CANVAS_SOURCE_SKIP",
                get_client_ip(),
                user=_actor_value(get_current_user_ctx() or {}, "username"),
                success=False,
                ua=get_ua(),
                detail=f"missing_source_cloud_file_id {msg}"[:240],
            )
            return next_body, ""
        source_path, path_msg = _resolve_uploaded_file_path_for_actor(source_cloud)
        if path_msg:
            audit(
                "AI_AGENT_QWEN_EDIT_CANVAS_SOURCE_SKIP",
                get_client_ip(),
                user=_actor_value(get_current_user_ctx() or {}, "username"),
                success=False,
                ua=get_ua(),
                detail=f"source_file_id={source_cloud} {path_msg}"[:240],
            )
            return next_body, ""
        try:
            from PIL import Image
            with Image.open(source_path) as probe:
                image_size = tuple(probe.size)
        except Exception as exc:
            return None, f"來源圖片尺寸讀取失敗：{exc}"
        if not _qwen_edit_source_canvas_needed(next_body, image_size):
            return next_body, ""

        canvas_bytes, original_size, canvas_msg = _render_qwen_edit_canvas(source_path, width=width, height=height)
        if canvas_msg:
            return None, canvas_msg
        filename = f"qwen_edit_canvas_{width}x{height}_{hashlib.sha1(canvas_bytes).hexdigest()[:12]}.png"
        backend_url = str(next_body.get("backend_url") or next_body.get("comfyui_backend_url") or "").strip()
        status_code, payload = _dispatch_internal_image_upload(filename, canvas_bytes, "image/png", backend_url=backend_url)
        if not (200 <= int(status_code or 500) < 400) or not isinstance(payload, dict) or not payload.get("ok"):
            msg = str((payload or {}).get("msg") or (payload or {}).get("raw") or "").strip() if isinstance(payload, dict) else ""
            return None, f"Qwen Edit 寬畫布來源圖匯入失敗{('：' + msg) if msg else ''}"
        image_info = payload.get("image") if isinstance(payload.get("image"), dict) else {}
        imported_ref = image_info.get("image_ref") if isinstance(image_info.get("image_ref"), dict) else {}
        if not imported_ref:
            return None, "Qwen Edit 寬畫布匯入成功但缺少 ComfyUI image_ref"
        updated_ref = dict(imported_ref)
        for key in ("cloud_file_id", "storage_file_id", "filename", "mime_type", "size_bytes"):
            if image_info.get(key) is not None:
                updated_ref[key] = image_info.get(key)
        next_body["source_image_ref"] = updated_ref
        next_body["source_image_ref_json"] = updated_ref
        audit(
            "AI_AGENT_QWEN_EDIT_CANVAS_SOURCE",
            get_client_ip(),
            user=_actor_value(get_current_user_ctx() or {}, "username"),
            success=True,
            ua=get_ua(),
            detail=(
                f"source_file_id={source_cloud} original={original_size[0]}x{original_size[1]} "
                f"canvas={width}x{height} new_file_id={updated_ref.get('cloud_file_id')}"
            ),
        )
        return next_body, ""

    def _workflow_json_from_preset(preset):
        workflow = preset.get("workflow_json") if isinstance(preset, dict) else {}
        if isinstance(workflow, str):
            try:
                workflow = json.loads(workflow)
            except Exception:
                workflow = {}
        return workflow if isinstance(workflow, dict) else {}

    def _workflow_protected_media_assignments(preset, body):
        workflow = _workflow_json_from_preset(preset)
        if not workflow:
            return {}, None
        workflow_id = _official_workflow_id_from_body(body)
        source_ref = _image_ref_from_body(body, "source_image_ref")
        mask_ref = _image_ref_from_body(body, "mask_image_ref")
        reference_ref = _image_ref_from_body(body, "reference_image_ref")
        control_ref = _image_ref_from_body(body, "control_image_ref")
        if not control_ref and isinstance((body or {}).get("controlnet"), dict):
            nested_control_ref = ((body or {}).get("controlnet") or {}).get("image_ref")
            control_ref = nested_control_ref if isinstance(nested_control_ref, dict) else {}
        source_cloud, source_save_payload = _resolve_cloud_file_id_for_image_ref(source_ref)
        mask_cloud, mask_save_payload = _resolve_cloud_file_id_for_image_ref(mask_ref)
        reference_cloud, reference_save_payload = _resolve_cloud_file_id_for_image_ref(reference_ref)
        control_cloud, control_save_payload = _resolve_cloud_file_id_for_image_ref(control_ref)
        assignments = {}
        missing = []
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            class_type = str(node.get("class_type") or "").strip()
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
            title = str(meta.get("title") or node.get("title") or "").strip().lower()
            is_reference_node = (
                _is_qwen_edit_workflow_id(workflow_id)
                and class_type == "LoadImage"
                and ("reference" in title or str(node_id) == "79")
            )
            is_controlnet_input_node = (
                workflow_id == "origin_qwen_image_controlnet_2512"
                and class_type == "LoadImage"
            )
            if class_type == "LoadImage" and "image" in inputs:
                if is_reference_node:
                    if reference_cloud:
                        assignments[str(node_id)] = reference_cloud
                    # Qwen Image Edit can run in single-image mode. Do not
                    # silently feed the source image into the reference node,
                    # or ordinary edits become unintended two-image edits.
                    continue
                elif is_controlnet_input_node and control_cloud:
                    assignments[str(node_id)] = control_cloud
                elif source_cloud:
                    assignments[str(node_id)] = source_cloud
                else:
                    missing.append(str(node_id))
            elif class_type == "LoadImageMask" and "image" in inputs:
                if mask_cloud:
                    assignments[str(node_id)] = mask_cloud
                else:
                    missing.append(str(node_id))
            elif class_type == "LoadVideo" and "file" in inputs:
                missing.append(str(node_id))
        if missing:
            save_error = source_save_payload or mask_save_payload or reference_save_payload or control_save_payload or {}
            msg = str(save_error.get("msg") or "").strip() if isinstance(save_error, dict) else ""
            suffix = f"：{msg}" if msg else ""
            return assignments, {
                "ok": False,
                "msg": f"官方 workflow 需要站內雲端圖片來源，無法從目前圖片引用取得 cloud_file_id{suffix}",
                "stage": "missing_source_cloud_file_id",
                "missing_media_nodes": missing,
                "source_image_ref": source_ref,
                "mask_image_ref": mask_ref,
                "reference_image_ref": reference_ref,
                "control_image_ref": control_ref,
                "save_error": save_error,
            }
        return assignments, None

    def _workflow_required_user_input_defaults(preset):
        defaults = {}
        for field in _workflow_fields_from_preset(preset):
            if not isinstance(field, dict) or not field.get("required"):
                continue
            node_id = _workflow_field_node_id(field)
            input_name = str(field.get("input_name") or "").strip()
            class_type = str(field.get("class_type") or "").strip()
            input_type = str(field.get("input_type") or "").strip()
            if not node_id or not input_name:
                continue
            if input_type == "file_picker" and class_type in {"LoadImage", "LoadImageMask"}:
                continue
            if "current_value" not in field:
                continue
            _workflow_node_input_patch(defaults, node_id, input_name, field.get("current_value"))
        return defaults

    def _workflow_request_scalar_fields(body):
        return {
            "steps": (body or {}).get("steps"),
            "cfg": (body or {}).get("cfg") if (body or {}).get("cfg") is not None else (body or {}).get("cfg_scale"),
            "seed": (body or {}).get("seed"),
            "denoise": (body or {}).get("denoise_strength"),
            "sampler_name": (body or {}).get("sampler_name") if (body or {}).get("sampler_name") is not None else (body or {}).get("sampler"),
            "scheduler": (body or {}).get("scheduler"),
            "left": (body or {}).get("outpaint_left"),
            "top": (body or {}).get("outpaint_top"),
            "right": (body or {}).get("outpaint_right"),
            "bottom": (body or {}).get("outpaint_bottom"),
            "feathering": (body or {}).get("outpaint_feathering"),
        }

    def _workflow_apply_analyzed_sampler_fallbacks(preset, user_inputs, scalar_fields, adjustments):
        workflow = _workflow_json_from_preset(preset)
        if not workflow:
            return
        try:
            analysis = analyze_workflow_json(workflow)
        except Exception:
            return
        for field in analysis.user_inputs:
            if field.class_type != "KSampler":
                continue
            if field.category not in {FieldCategory.NUMERIC, FieldCategory.SAMPLER}:
                continue
            input_name = str(field.input_name or "").strip()
            if input_name not in scalar_fields or _is_missing_arg(scalar_fields.get(input_name)):
                continue
            _workflow_node_input_patch(user_inputs, field.node_id, input_name, scalar_fields.get(input_name))
            adjustments.append({
                "code": "workflow_sampler_arg_applied",
                "node_id": str(field.node_id),
                "input_name": input_name,
                "value": scalar_fields.get(input_name),
            })

    def _workflow_apply_checkpoint_override(preset, body, user_inputs, adjustments):
        requested = str(
            (body or {}).get("checkpoint_name")
            or (body or {}).get("checkpoint")
            or (body or {}).get("model")
            or ""
        ).strip()
        if not requested:
            return
        workflow = _workflow_json_from_preset(preset)
        if not workflow:
            return
        applied = []
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            if str(node.get("class_type") or "").strip() != "CheckpointLoaderSimple":
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            if "ckpt_name" not in inputs:
                continue
            _workflow_node_input_patch(user_inputs, node_id, "ckpt_name", requested)
            applied.append(str(node_id))
        if applied:
            adjustments.append({
                "code": "workflow_checkpoint_arg_applied",
                "input_name": "ckpt_name",
                "node_ids": applied,
                "value": requested,
            })

    def _workflow_apply_qwen_edit_switch_policy(preset, body, user_inputs, adjustments):
        workflow_id = str((preset or {}).get("system_bundle_id") or "").strip()
        if not _is_qwen_edit_workflow_id(workflow_id):
            return
        requested_profile = str(
            (body or {}).get("qwen_edit_profile")
            or (body or {}).get("qwen_profile")
            or (body or {}).get("profile")
            or ""
        ).strip().lower()
        requested_steps = (body or {}).get("steps")
        requested_cfg = (body or {}).get("cfg") if (body or {}).get("cfg") is not None else (body or {}).get("cfg_scale")
        use_base_branch = requested_profile in {"base", "full", "slow", "quality"}
        workflow = _workflow_json_from_preset(preset)
        for node_id, node in workflow.items():
            if not isinstance(node, dict) or str(node.get("class_type") or "") != "ComfySwitchNode":
                continue
            meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
            title = str(meta.get("title") or node.get("title") or "").strip().lower()
            if title not in {"switch (model)", "switch (steps)", "switch (cfg)"}:
                continue
            _workflow_node_input_patch(user_inputs, node_id, "switch", False if use_base_branch else True)
            adjustments.append({
                "code": "qwen_edit_base_branch_selected" if use_base_branch else "qwen_edit_lightning_branch_enforced",
                "node_id": str(node_id),
                "reason": "explicit_profile_request" if use_base_branch else "default_fast_profile",
            })
        if use_base_branch:
            return
        clamped = False
        for node_id, patch in list(user_inputs.items()):
            if not isinstance(patch, dict):
                continue
            if "steps" in patch and patch.get("steps") != 4:
                patch["steps"] = 4
                clamped = True
            if "cfg" in patch and patch.get("cfg") != 1:
                patch["cfg"] = 1
                clamped = True
        if clamped or requested_steps is not None or requested_cfg is not None:
            adjustments.append({
                "code": "qwen_edit_lightning_sampler_clamped",
                "steps": 4,
                "cfg": 1,
                "requested_steps": requested_steps,
                "requested_cfg": requested_cfg,
            })

    def _workflow_apply_qwen_2512_controlnet_switch_policy(preset, body, user_inputs, adjustments):
        workflow_id = str((preset or {}).get("system_bundle_id") or "").strip()
        if workflow_id != "origin_qwen_image_controlnet_2512":
            return
        body = body or {}
        control_type = str(body.get("controlnet_type") or "").strip().lower()
        preprocessor = str(body.get("controlnet_preprocessor") or "").strip().lower()
        has_control_ref = bool(
            _image_ref_from_body(body, "control_image_ref")
            or (isinstance(body.get("controlnet"), dict) and (body.get("controlnet") or {}).get("image_ref"))
        )
        is_pose_control = (
            control_type in {"pose", "openpose", "sdpose"}
            or preprocessor in {"pose", "openpose", "sdpose", "none", "passthrough"}
            or has_control_ref
        )
        if not is_pose_control:
            return

        def _numeric_or_none(value):
            if value in (None, ""):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        requested_profile = str(
            body.get("qwen_controlnet_profile")
            or body.get("qwen_profile")
            or body.get("profile")
            or ""
        ).strip().lower()
        requested_steps = _numeric_or_none(body.get("steps"))
        requested_cfg = _numeric_or_none(body.get("cfg") if body.get("cfg") is not None else body.get("cfg_scale"))
        use_fast_branch = (
            requested_profile in {"fast", "lightning", "lite", "quick"}
            or (requested_steps is not None and requested_steps <= 4)
            or (requested_cfg is not None and requested_cfg <= 1.2)
        )
        workflow = _workflow_json_from_preset(preset)

        def _clean_number(value, *, integer=False):
            if value is None:
                return None
            if integer:
                return int(value)
            return int(value) if float(value).is_integer() else value

        def _controlnet_switch_nodes(node_ids, titles):
            matches = []
            for node_id, node in workflow.items():
                if not isinstance(node, dict) or str(node.get("class_type") or "") != "ComfySwitchNode":
                    continue
                if str(node_id) in node_ids:
                    matches.append((node_id, node))
            if matches:
                return matches
            for node_id, node in workflow.items():
                if not isinstance(node, dict) or str(node.get("class_type") or "") != "ComfySwitchNode":
                    continue
                meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
                title = str(meta.get("title") or node.get("title") or "").strip().lower()
                if title in titles:
                    matches.append((node_id, node))
            return matches

        def _patch_switch_false(node_ids, titles, value, code):
            if value is None:
                return
            for node_id, node in _controlnet_switch_nodes(node_ids, titles):
                inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
                if "on_false" not in inputs:
                    continue
                _workflow_node_input_patch(user_inputs, node_id, "on_false", value)
                adjustments.append({
                    "code": code,
                    "node_id": str(node_id),
                    "input_name": "on_false",
                    "value": value,
                    "reason": "pose_control_base_branch_requested_sampler_value",
                })

        _patch_switch_false(
            {"132"},
            {"switch (steps)"},
            _clean_number(requested_steps, integer=True),
            "qwen_2512_controlnet_steps_switch_false_applied",
        )
        _patch_switch_false(
            {"133"},
            {"switch (cfg)"},
            _clean_number(requested_cfg),
            "qwen_2512_controlnet_cfg_switch_false_applied",
        )

        control_input_map = (
            ("control_strength", "strength"),
            ("control_start", "start_percent"),
            ("control_end", "end_percent"),
        )
        for source_key, input_name in control_input_map:
            if body.get(source_key) in (None, ""):
                continue
            for node_id, node in workflow.items():
                if not isinstance(node, dict):
                    continue
                if str(node.get("class_type") or "") != "ControlNetApplyAdvanced":
                    continue
                inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
                if input_name not in inputs:
                    continue
                _workflow_node_input_patch(user_inputs, node_id, input_name, body.get(source_key))
                adjustments.append({
                    "code": "qwen_2512_controlnet_scalar_applied",
                    "node_id": str(node_id),
                    "input_name": input_name,
                    "value": body.get(source_key),
                })
        requested_width = _numeric_or_none(body.get("width") or body.get("output_width") or body.get("requested_width"))
        requested_height = _numeric_or_none(body.get("height") or body.get("output_height") or body.get("requested_height"))
        if requested_width and requested_height:
            megapixels = max(0.25, min(1.05, round((requested_width * requested_height) / 1_000_000, 3)))
            for node_id, node in workflow.items():
                if not isinstance(node, dict):
                    continue
                if str(node.get("class_type") or "") != "ResizeImageMaskNode":
                    continue
                inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
                if "resize_type.megapixels" not in inputs:
                    continue
                _workflow_node_input_patch(user_inputs, node_id, "resize_type.megapixels", megapixels)
                adjustments.append({
                    "code": "qwen_2512_controlnet_resize_megapixels_applied",
                    "node_id": str(node_id),
                    "input_name": "resize_type.megapixels",
                    "value": megapixels,
                    "requested_width": requested_width,
                    "requested_height": requested_height,
                })
        for node_id, node in workflow.items():
            if not isinstance(node, dict):
                continue
            if str(node.get("class_type") or "") != "UNETLoader":
                continue
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            model_name = str(inputs.get("unet_name") or inputs.get("model_name") or "").strip().lower()
            if "qwen_image_2512" not in model_name or "fp8_e4m3fn" not in model_name:
                continue
            if "weight_dtype" not in inputs:
                continue
            _workflow_node_input_patch(user_inputs, node_id, "weight_dtype", "fp8_e4m3fn")
            adjustments.append({
                "code": "qwen_2512_controlnet_fp8_dtype_applied",
                "node_id": str(node_id),
                "input_name": "weight_dtype",
                "value": "fp8_e4m3fn",
            })
        if not use_fast_branch:
            return

        switch_titles = {
            "switch (model)": "qwen_2512_controlnet_fast_model_branch_enforced",
            "switch (steps)": "qwen_2512_controlnet_fast_steps_branch_enforced",
            "switch (cfg)": "qwen_2512_controlnet_fast_cfg_branch_enforced",
        }
        switch_node_ids = {"132", "133", "134"}
        for node_id, node in workflow.items():
            if not isinstance(node, dict) or str(node.get("class_type") or "") != "ComfySwitchNode":
                continue
            meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
            title = str(meta.get("title") or node.get("title") or "").strip().lower()
            if str(node_id) not in switch_node_ids and title not in switch_titles:
                continue
            _workflow_node_input_patch(user_inputs, node_id, "switch", True)
            adjustments.append({
                "code": switch_titles.get(title, "qwen_2512_controlnet_fast_branch_enforced"),
                "node_id": str(node_id),
                "reason": "pose_control_fast_profile",
            })

    def _workflow_qwen_edit_prompt(body):
        workflow_id = _official_workflow_id_from_body(body)
        if not _is_qwen_edit_workflow_id(workflow_id):
            return str((body or {}).get("prompt") or "").strip(), []
        edit_instruction = str(
            (body or {}).get("edit_instruction")
            or (body or {}).get("edit_prompt")
            or ""
        ).strip()
        prompt = str((body or {}).get("prompt") or "").strip()
        if not edit_instruction:
            return prompt, []
        style_context = prompt
        lower_instruction = edit_instruction.lower()
        style_shift_to_realistic = (
            "semi-realistic" in lower_instruction
            or "realistic" in lower_instruction
            or "photoreal" in lower_instruction
            or "寫實" in edit_instruction
            or "真實" in edit_instruction
        )
        adjustments = [{
            "code": "qwen_edit_instruction_prompt_applied",
            "source": "edit_instruction",
        }]
        if style_shift_to_realistic and re.search(r"\banime\s+style\b|\bby\s+ogipote\b", style_context, re.IGNORECASE):
            style_context = ""
            adjustments.append({
                "code": "qwen_edit_style_context_omitted",
                "reason": "realistic_style_shift",
            })
        elif style_context:
            sanitized_style_context = _sanitize_qwen_edit_style_context(style_context)
            if sanitized_style_context != style_context:
                style_context = sanitized_style_context
                adjustments.append({
                    "code": "qwen_edit_style_context_sanitized",
                    "reason": "avoid_visible_artist_text",
                })
        if style_context and edit_instruction not in style_context:
            combined = (
                f"{edit_instruction}\n\n"
                f"Style and preservation context: {style_context}"
            )
        else:
            combined = edit_instruction
        return combined, adjustments

    def _qwen_edit_prompt_needs_instruction(body):
        workflow_id = _official_workflow_id_from_body(body)
        if not _is_qwen_edit_workflow_id(workflow_id):
            return False
        mode = str((body or {}).get("generation_mode") or "").strip().lower()
        if mode and mode != "img2img":
            return False
        edit_instruction = str(
            (body or {}).get("edit_instruction")
            or (body or {}).get("edit_prompt")
            or ""
        ).strip()
        if edit_instruction:
            return False
        prompt = str((body or {}).get("prompt") or "").strip()
        if not prompt:
            return True
        compact = re.sub(r"[\W_]+", "", prompt.lower())
        style_only = compact in {
            "byogipoteanimestyle1girl",
            "animestyle1girl",
            "1girl",
        }
        if style_only:
            return True
        return not re.search(
            r"\b(replace|remove|delete|change|edit|modify|fix|repair|transform|turn|convert|keep|preserve|保持|保留|替換|改成|移除|刪除|修正|修復|換成)\b",
            prompt,
            re.IGNORECASE,
        )

    def _workflow_user_inputs_from_generate_body(preset, body):
        user_inputs = _workflow_required_user_input_defaults(preset)
        fields = _workflow_fields_from_preset(preset)
        prompt, prompt_adjustments = _workflow_qwen_edit_prompt(body)
        negative = str((body or {}).get("negative_prompt") or "").strip()
        default_negative = "low quality, worst quality, text, watermark, logo"
        scalar_fields = _workflow_request_scalar_fields(body)
        adjustments = list(prompt_adjustments)
        for field in fields:
            node_id = _workflow_field_node_id(field)
            input_name = str(field.get("input_name") or "").strip()
            if not node_id or not input_name:
                continue
            class_type = str(field.get("class_type") or "")
            label = str(field.get("label") or "").lower()
            input_type = str(field.get("input_type") or "")
            if input_type == "file_picker" and class_type in {"LoadImage", "LoadImageMask"}:
                continue
            if input_type == "textarea" and prompt and ("negative" not in label and "負面" not in label):
                _workflow_node_input_patch(user_inputs, node_id, input_name, prompt)
            elif input_type == "textarea" and negative and ("negative" in label or "負面" in label):
                _workflow_node_input_patch(user_inputs, node_id, input_name, negative)
            elif (
                input_type == "textarea"
                and field.get("required")
                and ("negative" in label or "負面" in label)
            ):
                _workflow_node_input_patch(user_inputs, node_id, input_name, default_negative)
                adjustments.append({
                    "code": "workflow_required_negative_prompt_defaulted",
                    "node_id": str(node_id),
                    "input_name": input_name,
                    "value": default_negative,
                })
            elif input_name in scalar_fields:
                _workflow_node_input_patch(user_inputs, node_id, input_name, scalar_fields.get(input_name))
        _workflow_apply_analyzed_sampler_fallbacks(preset, user_inputs, scalar_fields, adjustments)
        _workflow_apply_checkpoint_override(preset, body, user_inputs, adjustments)
        _workflow_apply_qwen_edit_switch_policy(preset, body, user_inputs, adjustments)
        _workflow_apply_qwen_2512_controlnet_switch_policy(preset, body, user_inputs, adjustments)
        workflow_id = str((preset or {}).get("system_bundle_id") or "").strip()
        reference_ref = _image_ref_from_body(body, "reference_image_ref")
        if _is_qwen_edit_workflow_id(workflow_id) and reference_ref:
            adjustments.append({
                "code": "qwen_edit_reference_image_link_expected",
                "node_id": "494",
                "input_name": "image2",
                "reference_node_id": "79",
            })
        return user_inputs, adjustments

    def _workflow_dependency_error(preset):
        status = (preset or {}).get("dependency_status") if isinstance(preset, dict) else None
        if not isinstance(status, dict):
            return None
        missing_keys = ("missing_nodes", "missing_models", "missing_loras", "missing_controlnets", "missing_custom_nodes")
        missing = {key: status.get(key) for key in missing_keys if status.get(key)}
        if not missing:
            return None
        return {
            "ok": False,
            "msg": "官方 ComfyUI workflow 依賴尚未安裝，已取消送出，避免退回錯誤的快捷 workflow。",
            "stage": "missing_workflow_dependency",
            "dependency_status": status,
            "missing": missing,
        }

    def _dispatch_official_comfyui_workflow(body):
        workflow_id = _official_workflow_id_from_body(body)
        status_code, workflows_payload = _dispatch_internal_api("GET", "/api/comfyui/workflows", None)
        if not (200 <= int(status_code or 500) < 400) or not isinstance(workflows_payload, dict) or not workflows_payload.get("ok"):
            return status_code, workflows_payload
        presets = workflows_payload.get("presets") or workflows_payload.get("official_presets") or []
        preset_summary = next(
            (
                item for item in presets
                if isinstance(item, dict) and str(item.get("system_bundle_id") or "").strip() == workflow_id
            ),
            None,
        )
        if not preset_summary:
            return 404, {"ok": False, "msg": f"找不到官方 ComfyUI workflow：{workflow_id}", "stage": "workflow_not_found"}
        preset_id = int(preset_summary.get("id") or 0)
        detail_status, detail_payload = _dispatch_internal_api("GET", f"/api/comfyui/workflows/{preset_id}", None)
        if not (200 <= int(detail_status or 500) < 400) or not isinstance(detail_payload, dict) or not detail_payload.get("ok"):
            return detail_status, detail_payload
        preset = detail_payload.get("preset") if isinstance(detail_payload.get("preset"), dict) else {}
        dependency_error = _workflow_dependency_error(preset)
        if dependency_error:
            return 409, dependency_error
        mode = str((body or {}).get("generation_mode") or "").strip().lower()
        if mode in {"img2img", "inpaint", "outpaint"} and not _source_image_filename(body):
            return 400, {"ok": False, "msg": "官方圖片編輯 workflow 需要來源圖片", "stage": "missing_source_image"}
        if _qwen_edit_prompt_needs_instruction(body):
            return 400, {
                "ok": False,
                "msg": "Qwen Image Edit 需要明確的語意編輯命令；請提供 edit_instruction，或把 prompt 寫成 replace/remove/change/fix 類的直接編輯指令。",
                "stage": "missing_qwen_edit_instruction",
            }
        body, single_reference_adjustments = _strip_qwen_single_semantic_reference_image(body)
        image_assignments, image_assignment_error = _workflow_protected_media_assignments(preset, body)
        if image_assignment_error:
            return 400, image_assignment_error
        user_inputs, workflow_adjustments = _workflow_user_inputs_from_generate_body(preset, body)
        workflow_adjustments = list(workflow_adjustments or []) + list(single_reference_adjustments or [])
        run_body = {
            "user_inputs": user_inputs,
            "image_field_assignments": image_assignments,
            "run_count": body.get("batch_size") or body.get("run_count") or 1,
            "seed_after_generate": "fixed",
        }
        for key in ("backend_url", "comfyui_backend_url"):
            if body.get(key) not in (None, ""):
                run_body[key] = body.get(key)
        for key in (
            "source_image_ref",
            "source_image_ref_json",
            "mask_image_ref",
            "mask_image_ref_json",
            "reference_image_ref",
            "reference_image_ref_json",
            "pose_reference_image_ref",
            "control_image_ref",
            "control_image_ref_json",
        ):
            if isinstance((body or {}).get(key), dict):
                run_body[key] = body.get(key)
        for source_key, target_key in (
            ("width", "width"),
            ("height", "height"),
            ("output_width", "output_width"),
            ("output_height", "output_height"),
            ("requested_width", "requested_width"),
            ("requested_height", "requested_height"),
        ):
            if body.get(source_key) not in (None, ""):
                run_body[target_key] = body.get(source_key)
        if body.get("vae") or body.get("vae_name"):
            run_body["vae"] = body.get("vae") or body.get("vae_name")
        for key in (
            "controlnet_type",
            "controlnet_preprocessor",
            "controlnet_model",
            "control_strength",
            "control_start",
            "control_end",
            "qwen_controlnet_profile",
        ):
            if body.get(key) not in (None, ""):
                run_body[key] = body.get(key)
        run_status, run_payload = _dispatch_internal_api("POST", f"/api/comfyui/workflows/{preset_id}/run", run_body)
        if isinstance(run_payload, dict):
            run_payload.setdefault("official_workflow_id", workflow_id)
            if workflow_adjustments:
                run_payload.setdefault("workflow_bridge_adjustments", workflow_adjustments)
        return run_status, run_payload

    def _launch_step_ok(status_code, payload):
        if not (200 <= int(status_code or 500) < 400):
            return False
        if isinstance(payload, dict) and payload.get("ok") is False:
            return False
        return True

    def _launch_requirement_blockers(payload):
        if not isinstance(payload, dict):
            return ["requirements payload 格式錯誤"]
        blockers = []
        missing = list(payload.get("missing") or [])
        failed = list(payload.get("failed") or [])
        if missing:
            blockers.append(f"缺少 production gate 報告：{', '.join(str(item) for item in missing)}")
        if failed:
            blockers.append(f"production gate 報告未通過：{', '.join(str(item) for item in failed)}")
        reports = payload.get("reports") if isinstance(payload.get("reports"), dict) else {}
        for report_type in failed:
            row = reports.get(report_type) if isinstance(reports, dict) else None
            if isinstance(row, dict):
                details = []
                if not bool(row.get("pass")):
                    details.append("test_result 不是 pass")
                if int(row.get("critical_findings_count") or 0) > 0:
                    details.append(f"critical={int(row.get('critical_findings_count') or 0)}")
                if int(row.get("high_findings_count") or 0) > 0:
                    details.append(f"high={int(row.get('high_findings_count') or 0)}")
                if not row.get("report_hash"):
                    details.append("缺 report_hash")
                if str(row.get("trust_level") or "").strip() != "verified":
                    details.append("trust_level 不是 verified")
                if not bool(row.get("signature_valid")):
                    details.append("signature 無效")
                if not bool(row.get("target_match")):
                    details.append("target 不符合目前版本/模式")
                if details:
                    blockers.append(f"{report_type}: {', '.join(details)}")
        if payload.get("ok") is False and not blockers:
            blockers.append(str(payload.get("msg") or "production requirements 未通過"))
        return blockers

    def _launch_preflight_summary(requirements_payload, logs_payload, audit_payload, switch_payload=None):
        blockers = []
        blockers.extend(_launch_requirement_blockers(requirements_payload))
        if isinstance(logs_payload, dict) and logs_payload.get("ok") is False:
            blockers.append(str(logs_payload.get("msg") or "server-mode log chain 驗證失敗"))
        if isinstance(audit_payload, dict):
            scan = audit_payload.get("scan") if "scan" in audit_payload else audit_payload
            if isinstance(scan, dict):
                summary = scan.get("summary") if isinstance(scan.get("summary"), dict) else {}
                status = str(summary.get("status") or scan.get("status") or "").strip().lower()
                anomalies = int(summary.get("anomaly_count") or 0)
                if status in {"critical", "alert", "failed", "error"}:
                    blockers.append(f"AI audit scan 狀態異常：{status}")
                elif anomalies > 0:
                    blockers.append(f"AI audit scan 發現異常：{anomalies}")
        if isinstance(switch_payload, dict) and switch_payload.get("ok") is False:
            blockers.append(str(switch_payload.get("msg") or "server mode 切換失敗"))
        return blockers

    def _execute_launch_preflight(actor, args, settings):
        target_mode = str(args.get("target_mode") or args.get("mode") or "production").strip().lower()
        if target_mode in {"prod", "online", "上線", "正式", "go_live", "golive"}:
            target_mode = "production"
        if target_mode != "production":
            return 400, {
                "ok": False,
                "msg": "目前自動上線流程只支援 target_mode=production",
                "target_mode": target_mode,
            }
        auto_switch = args.get("auto_switch")
        if auto_switch is None:
            auto_switch = True
        auto_switch = _parse_bool(auto_switch) is not False
        force_audit = _parse_bool(args.get("force_audit")) is not False
        reason = str(args.get("reason") or "AI Agent launch preflight").strip()[:300]

        steps = []
        req_status, req_payload = _dispatch_internal_api("GET", "/api/root/server-mode/requirements", None)
        steps.append({
            "name": "requirements_gate",
            "status": req_status,
            "ok": _launch_step_ok(req_status, req_payload),
            "result": req_payload,
        })
        logs_status, logs_payload = _dispatch_internal_api("GET", "/api/root/server-mode/logs/verify", None)
        steps.append({
            "name": "log_chain_verify",
            "status": logs_status,
            "ok": _launch_step_ok(logs_status, logs_payload),
            "result": logs_payload,
        })
        audit_payload = run_ai_agent_audit_scan(
            settings,
            get_db=get_db,
            get_audit_db=get_audit_db,
            actor=actor,
            force=bool(force_audit),
            get_client_ip=get_client_ip,
            get_ua=get_ua,
            audit=audit,
        )
        steps.append({
            "name": "ai_agent_audit_scan",
            "status": 200,
            "ok": True,
            "result": audit_payload,
        })

        blockers = _launch_preflight_summary(req_payload, logs_payload, audit_payload)
        switch_payload = None
        switch_status = None
        if auto_switch and not blockers:
            confirm = str(args.get("confirm") or "GO_LIVE").strip()
            switch_status, switch_payload = _dispatch_internal_api("POST", "/api/root/server-mode/switch", {
                "mode": "production",
                "confirm": confirm,
                "reason": reason,
            })
            steps.append({
                "name": "switch_production",
                "status": switch_status,
                "ok": _launch_step_ok(switch_status, switch_payload),
                "result": switch_payload,
            })
            blockers = _launch_preflight_summary(req_payload, logs_payload, audit_payload, switch_payload)
        elif auto_switch:
            steps.append({
                "name": "switch_production",
                "status": 0,
                "ok": False,
                "skipped": True,
                "result": {
                    "ok": False,
                    "msg": "前置檢查未通過，未切換 production",
                },
            })

        final_status, final_mode_payload = _dispatch_internal_api("GET", "/api/root/server-mode", None)
        final_mode = ""
        if isinstance(final_mode_payload, dict):
            final_mode = str(final_mode_payload.get("mode") or "")
        completed = final_mode == "production" and not blockers
        return 200, {
            "ok": True,
            "completed": completed,
            "target_mode": "production",
            "final_mode": final_mode,
            "auto_switch": bool(auto_switch),
            "blockers": blockers,
            "steps": steps,
            "final_status": {
                "status": final_status,
                "result": final_mode_payload,
            },
            "msg": "已成功切換 production" if completed else "上線流程已執行，但尚未達成 production；請依 blockers 修正後重跑",
        }

    def _dispatch_internal_multipart(path, data):
        headers = {}
        csrf = request.headers.get("X-CSRF-Token") or request.headers.get("X-CSRFToken") or request.cookies.get("csrf_token") or ""
        if csrf:
            headers["X-CSRF-Token"] = csrf
        with app.test_client() as client:
            for name, value in request.cookies.items():
                client.set_cookie(str(name), str(value))
            response = client.post(
                path,
                data=data,
                headers=headers,
                content_type="multipart/form-data",
                environ_base={"hackme.internal_dispatch": "ai_agent_write_tool"},
            )
        payload = response.get_json(silent=True)
        if payload is None:
            payload = {"raw": response.get_data(as_text=True)[:4000]}
        return response.status_code, payload

    def _codex_handoff_json(value, *, max_chars=12000):
        try:
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            raw = json.dumps(str(value or ""), ensure_ascii=False)
        if len(raw) <= max_chars:
            return raw
        return json.dumps({
            "truncated": True,
            "preview": raw[:max_chars],
            "omitted_chars": len(raw) - max_chars,
        }, ensure_ascii=False, sort_keys=True)

    def _codex_handoff_text(value, limit):
        return str(value or "").strip()[:limit]

    def _codex_handoff_path_warnings(*values):
        warnings = []
        for value in values:
            if isinstance(value, (dict, list, tuple)):
                text = _codex_handoff_json(value, max_chars=16000)
            else:
                text = str(value or "")
            outside_paths = _ai_agent_os_paths_outside_runtime(text)
            if outside_paths:
                preview = ", ".join(outside_paths[:4])
                warnings.append(f"contains_server_path_outside_runtime:{preview}")
        return warnings

    def _execute_codex_handoff_create(actor, args):
        objective = _codex_handoff_text(args.get("objective"), 6000)
        if not objective:
            return 400, {"ok": False, "msg": "objective 必填"}
        title = _codex_handoff_text(args.get("title"), 160)
        if not title:
            title = objective.splitlines()[0][:120] or "Codex handoff"
        priority = _codex_handoff_text(args.get("priority") or "normal", 24).lower()
        if priority not in {"low", "normal", "high", "urgent"}:
            priority = "normal"
        allowed_scope = _codex_handoff_text(args.get("allowed_scope") or "runtime_and_cloud_drive_only", 500)
        safety_notes = _codex_handoff_text(args.get("safety_notes"), 2000)
        context = args.get("context") if "context" in args else {}
        requested_artifacts = args.get("requested_artifacts") if "requested_artifacts" in args else []
        source_conversation_id = _conversation_id(args.get("source_conversation_id") or "")
        if source_conversation_id == "default" and not args.get("source_conversation_id"):
            source_conversation_id = ""

        warnings = _codex_handoff_path_warnings(
            title, objective, context, allowed_scope, requested_artifacts, safety_notes,
        )
        if warnings:
            safety_notes = (safety_notes + "\n" if safety_notes else "") + "\n".join(warnings)
        status = "needs_review" if warnings else "queued"
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        seed = json.dumps({
            "actor": _actor_value(actor, "username", ""),
            "objective": objective,
            "created_at": now,
            "scope": allowed_scope,
        }, ensure_ascii=False, sort_keys=True)
        handoff_id = "codex-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]

        conn = get_db()
        try:
            _ensure_ai_agent_codex_handoff_schema(conn)
            conn.execute(
                """
                INSERT INTO ai_agent_codex_handoffs (
                    id, owner_user_id, owner_username, status, priority, title, objective,
                    context_json, allowed_scope, requested_artifacts_json, safety_notes,
                    source_conversation_id, source_session_binding, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_id,
                    int(_actor_value(actor, "id", 0) or 0),
                    str(_actor_value(actor, "username", "") or ""),
                    status,
                    priority,
                    title,
                    objective,
                    _codex_handoff_json(context),
                    allowed_scope,
                    _codex_handoff_json(requested_artifacts, max_chars=4000),
                    safety_notes,
                    source_conversation_id,
                    _conversation_binding(),
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return 200, {
            "ok": True,
            "handoff": {
                "id": handoff_id,
                "status": status,
                "priority": priority,
                "title": title,
                "objective": objective,
                "allowed_scope": allowed_scope,
                "warnings": warnings,
                "created_at": now,
            },
            "msg": "已建立 Codex 交接任務；此接口只排程與紀錄，需由 root/Codex 審核後才會執行。",
        }

    def _execute_share_create(actor, args):
        if not str(args.get("storage_file_id") or args.get("file_id") or "").strip():
            return 400, {"ok": False, "msg": "storage_file_id 或 file_id 至少需要一個"}
        conn = get_db()
        try:
            link, msg = create_share_link(
                conn,
                actor=actor,
                storage_file_id=args.get("storage_file_id"),
                file_id=args.get("file_id"),
                expires_at=args.get("expires_at"),
                can_preview=bool(args.get("can_preview", True)),
                access_scope=args.get("access_scope") or "link",
                required_user_id=args.get("required_user_id"),
                required_username=args.get("required_username"),
                max_views=args.get("max_views") or 0,
                wrapped_file_key_envelope=args.get("wrapped_file_key_envelope"),
                share_password=args.get("share_password"),
            )
            if msg:
                conn.rollback()
                return 400, {"ok": False, "msg": msg}
            conn.commit()
            return 200, {"ok": True, "share": link}
        finally:
            conn.close()

    def _execute_subtitle_upload(args):
        try:
            video_id = int(args.get("video_id") or 0)
        except Exception:
            video_id = 0
        if video_id <= 0:
            return 400, {"ok": False, "msg": "video_id 必須是正整數"}
        text = str(args.get("subtitle_text") or "")
        if not text.strip():
            return 400, {"ok": False, "msg": "subtitle_text 不可為空"}
        raw = text.encode("utf-8")
        if len(raw) > 512 * 1024:
            return 400, {"ok": False, "msg": "subtitle_text 目前限制 512KB 以內"}
        filename = str(args.get("filename") or "ai-agent-subtitle.vtt").strip() or "ai-agent-subtitle.vtt"
        if not re.search(r"\.(srt|vtt|ass|ssa)$", filename, re.I):
            filename += ".vtt"
        form_data = {
            "subtitle": (io.BytesIO(raw), filename),
            "label": str(args.get("label") or "")[:80],
            "language": str(args.get("language") or "und")[:16] or "und",
        }
        return _dispatch_internal_multipart(f"/api/videos/{video_id}/subtitles", form_data)

    def _avatar_ai_decision_payload(args, crop):
        payload = {
            "crop": crop or {},
            "zoom": args.get("zoom"),
            "decision_reason": str(args.get("decision_reason") or "")[:500],
        }
        for key in (
            "confidence", "subject_detected", "crop_quality", "issues", "target_description",
            "preflight_ok", "preflight_crop_quality", "preflight_issues",
            "final_avatar_ok", "final_crop_quality", "final_issues",
        ):
            if key in args:
                payload[key] = args.get(key)
        return payload

    def _avatar_ai_decision_reject_reason(args):
        visual_keys = (
            "confidence", "subject_detected", "crop_quality", "decision_reason", "issues",
            "preflight_ok", "preflight_crop_quality", "preflight_issues",
            "final_avatar_ok", "final_crop_quality", "final_issues",
        )
        if not any(key in args for key in visual_keys):
            return ""
        explicit_bool_checks = (
            ("preflight_ok", "視覺預檢判斷裁切後頭像不合格"),
            ("final_avatar_ok", "最終視覺驗證判斷頭像不合格"),
        )
        for key, message in explicit_bool_checks:
            value = args.get(key)
            if value is False or str(value or "").strip().lower() in {"false", "no", "0"}:
                return message
        subject_detected = args.get("subject_detected")
        if subject_detected is False or str(subject_detected or "").strip().lower() in {"false", "no", "0"}:
            return "未偵測到清晰頭像主體"
        try:
            confidence = float(args.get("confidence"))
        except Exception:
            confidence = None
        if confidence is not None and confidence < 0.55:
            return f"視覺信心過低（confidence={confidence:.2f}）"
        bad_quality_values = {"poor", "invalid", "bad", "unusable", "low", "needs_adjustment", "off_center", "text_interference"}
        for key, label in (
            ("crop_quality", "crop_quality"),
            ("preflight_crop_quality", "preflight_crop_quality"),
            ("final_crop_quality", "final_crop_quality"),
        ):
            crop_quality = str(args.get(key) or "").strip().lower()
            if crop_quality in bad_quality_values:
                return f"裁切品質不合格（{label}={crop_quality}）"
        issues = args.get("issues")
        preflight_issues = args.get("preflight_issues")
        final_issues = args.get("final_issues")
        issue_parts = []
        for value in (issues, preflight_issues, final_issues):
            issue_parts.append(" ".join(str(item) for item in value) if isinstance(value, list) else str(value or ""))
        issue_text = " ".join(issue_parts)
        negative_text = " ".join([
            str(args.get("decision_reason") or ""),
            issue_text,
        ]).lower()
        negative_markers = (
            "no discernible human",
            "no clear human",
            "no visible subject",
            "no face",
            "not a portrait",
            "blank image",
            "face_off_center",
            "off center",
            "off-center",
            "excessive_whitespace",
            "text_interference",
            "watermark",
            "無清晰人像",
            "沒有清晰人像",
            "沒有主體",
            "無主體",
            "不適合作為頭像",
        )
        if any(marker in negative_text for marker in negative_markers):
            return "AI 判斷圖片沒有可用的人像主體"
        return ""

    def _execute_member_avatar_from_cloud(args):
        try:
            user_id = int(args.get("user_id") or 0)
        except Exception:
            user_id = 0
        if user_id <= 0:
            return 400, {"ok": False, "msg": "user_id 必須是正整數"}
        cloud_file_id = str(args.get("cloud_file_id") or args.get("existing_file_id") or "").strip()
        if not cloud_file_id:
            return 400, {"ok": False, "msg": "cloud_file_id 必填"}
        reject_reason = _avatar_ai_decision_reject_reason(args)
        if reject_reason:
            return 400, {
                "ok": False,
                "msg": f"AI 視覺判斷此圖片不適合作為頭像：{reject_reason}",
                "avatar_ai_decision": _avatar_ai_decision_payload(args, {}),
            }
        crop = args.get("crop") if isinstance(args.get("crop"), dict) else None
        crop_json = args.get("crop_json")
        if crop is None and isinstance(crop_json, str) and crop_json.strip():
            try:
                parsed = json.loads(crop_json)
                crop = parsed if isinstance(parsed, dict) else None
            except Exception:
                crop = None
        if crop is None:
            crop = {}
            for key in ("x", "y", "width", "height", "rotation"):
                if key in args:
                    crop[key] = args.get(key)
        form_data = {
            "cloud_file_id": cloud_file_id,
            "crop_json": json.dumps(crop or {}, ensure_ascii=False),
        }
        status, payload = _dispatch_internal_multipart(f"/api/admin/users/{user_id}/avatar", form_data)
        if isinstance(payload, dict):
            payload.setdefault("avatar_ai_decision", _avatar_ai_decision_payload(args, crop or {}))
        return status, payload

    def _safe_tool_payload(payload, *, max_chars=16000):
        try:
            raw = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            raw = str(payload)
        if len(raw) <= max_chars:
            return payload
        return {
            "truncated": True,
            "preview": raw[:max_chars],
            "omitted_chars": len(raw) - max_chars,
        }

    def _tool_payload_error_summary(payload):
        if not isinstance(payload, dict):
            return ""
        candidates = [
            payload.get("msg"),
            payload.get("message"),
            payload.get("error"),
        ]
        nested = payload.get("result")
        if isinstance(nested, dict):
            candidates.extend([nested.get("msg"), nested.get("message"), nested.get("error")])
        nested = payload.get("payload")
        if isinstance(nested, dict):
            candidates.extend([nested.get("msg"), nested.get("message"), nested.get("error")])
        for item in candidates:
            text = str(item or "").strip()
            if text:
                return text[:180]
        return ""

    @app.route("/api/ai-agent/codex-handoffs", methods=["GET"])
    @require_csrf_safe
    def ai_agent_codex_handoffs_route():
        actor, denied = _require_write_tool_actor()
        if denied:
            return denied
        limit = _coerce_limit(request.args.get("limit", "20"))
        include_context = _parse_bool(request.args.get("include_context")) is True
        status = str(request.args.get("status") or "").strip().lower()
        params = []
        where = ""
        if status:
            where = "WHERE status=?"
            params.append(status)
        conn = get_db()
        try:
            _ensure_ai_agent_codex_handoff_schema(conn)
            rows = conn.execute(
                f"""
                SELECT id, owner_user_id, owner_username, status, priority, title, objective,
                       context_json, allowed_scope, requested_artifacts_json, safety_notes,
                       source_conversation_id, created_at, updated_at
                FROM ai_agent_codex_handoffs
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (*params, limit),
            ).fetchall()
        finally:
            conn.close()
        items = []
        for row in rows:
            item = {
                "id": row["id"],
                "owner_user_id": row["owner_user_id"],
                "owner_username": row["owner_username"],
                "status": row["status"],
                "priority": row["priority"],
                "title": row["title"],
                "objective": row["objective"],
                "allowed_scope": row["allowed_scope"],
                "requested_artifacts": json.loads(row["requested_artifacts_json"] or "[]"),
                "safety_notes": row["safety_notes"],
                "source_conversation_id": row["source_conversation_id"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            if include_context:
                item["context"] = json.loads(row["context_json"] or "{}")
            items.append(item)
        _audit_agent_event("AI_AGENT_CODEX_HANDOFFS_LIST", actor, success=True, detail=f"count={len(items)},status={status or 'all'}")
        return json_resp({"ok": True, "handoffs": items})

    @app.route("/api/ai-agent/write-tools", methods=["GET"])
    @require_csrf_safe
    def ai_agent_write_tools_route():
        actor, denied = _require_write_tool_actor()
        if denied:
            return denied
        include_all = _parse_bool(request.args.get("include_all")) is True
        guard = ai_agent_write_guard_status(get_db=get_audit_db)
        guard_denied = _ai_agent_write_guard_denied(actor, endpoint="list") if guard.get("blocked") and not include_all else None
        if guard_denied:
            return guard_denied
        settings = get_system_settings() or {}
        public = public_ai_agent_settings(settings, actor=actor)
        effective_names = _write_tool_effective_names(settings, actor)
        tools = [
            _write_tool_public_spec(name, spec)
            for name, spec in AI_AGENT_WRITE_TOOL_SPECS.items()
            if name in effective_names
        ]
        catalog_tools = [
            _write_tool_public_spec(name, spec)
            for name, spec in AI_AGENT_WRITE_TOOL_SPECS.items()
        ] if include_all else []
        catalog_sha256 = _write_tool_catalog_fingerprint(tools)
        _audit_agent_event(
            "AI_AGENT_WRITE_TOOLS_LIST",
            actor,
            success=True,
            detail=f"mode={public.get('operation_mode')},tools={len(tools)},include_all={include_all},catalog_sha256={catalog_sha256}",
        )
        return json_resp({
            "ok": True,
            "root_only": True,
            "operation_mode": public.get("operation_mode"),
            "write_enabled": bool((public.get("operation_mode_policy") or {}).get("write_enabled")),
            "guard": guard,
            "catalog_sha256": catalog_sha256,
            "allowed_tools": public.get("allowed_tools") or "",
            "catalog_tools": catalog_tools,
            "tools": tools,
        })

    @app.route("/api/ai-agent/write-tools/execute", methods=["POST"])
    @require_csrf
    def ai_agent_write_tool_execute_route():
        actor, denied = _require_write_tool_actor()
        if denied:
            return denied
        guard_denied = _ai_agent_write_guard_denied(actor, endpoint="execute")
        if guard_denied:
            return guard_denied
        data, bad_request = _request_json_dict()
        if bad_request:
            return bad_request
        tool_name = str(data.get("tool") or "").strip()
        spec = AI_AGENT_WRITE_TOOL_SPECS.get(tool_name)
        if not spec:
            _audit_agent_event("AI_AGENT_WRITE_TOOL", actor, success=False, detail=f"tool={tool_name or '-'},error=unsupported_tool")
            return json_resp({"ok": False, "msg": "不支援的 write tool"}), 400
        args = data.get("arguments")
        if args is None:
            args = data.get("params")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            _audit_agent_event("AI_AGENT_WRITE_TOOL", actor, success=False, detail=f"tool={tool_name},error=arguments_not_object")
            return json_resp({"ok": False, "msg": "arguments 必須是物件"}), 400
        boundary_reason = _ai_agent_write_tool_boundary_block_reason(tool_name, args)
        if boundary_reason:
            _audit_agent_event("AI_AGENT_BOUNDARY_BLOCK", actor, success=False, detail=boundary_reason)
            return json_resp({
                "ok": False,
                "msg": "AI Agent write-tools 不可修改伺服器本體檔案；允許範圍僅限站內 runtime、資料庫與雲端硬碟管理位置。",
                "blocked_by": "server_policy",
                "policy": "server_filesystem_mutation",
            }), 403

        settings = get_system_settings() or {}
        effective_names = _write_tool_effective_names(settings, actor)
        if tool_name not in effective_names:
            _audit_agent_event("AI_AGENT_WRITE_TOOL", actor, success=False, detail=f"tool={tool_name},error=tool_not_allowed")
            return json_resp({"ok": False, "msg": "此工具未在目前 AI Agent allowed_tools/角色範圍內啟用"}), 403

        public = public_ai_agent_settings(settings, actor=actor)
        write_enabled = bool((public.get("operation_mode_policy") or {}).get("write_enabled"))
        elevate_once = data.get("elevate_once") in {True, "ALLOW_WRITE_ONCE", "allow_write_once"}
        if spec.get("write") and not write_enabled and not (_actor_is_super_admin(actor) and elevate_once):
            _audit_agent_event("AI_AGENT_WRITE_TOOL", actor, success=False, detail=f"tool={tool_name},error=operation_mode_not_write,mode={public.get('operation_mode')}")
            return json_resp({
                "ok": False,
                "msg": "寫入型工具需要 root 允許本次提權，或先將 AI Agent operation mode 切換為 write",
                "operation_mode": public.get("operation_mode"),
                "requires_elevation": _actor_is_super_admin(actor),
            }), 409
        if spec.get("write") and data.get("confirm") not in {True, "EXECUTE", "execute"}:
            _audit_agent_event("AI_AGENT_WRITE_TOOL", actor, success=False, detail=f"tool={tool_name},error=missing_confirm")
            return json_resp({"ok": False, "msg": "寫入型工具需要 confirm=true 或 confirm=\"EXECUTE\""}), 400

        status_code = 200
        try:
            if spec.get("method") == "DIRECT":
                if tool_name == "audit_scan":
                    force = _parse_bool(args.get("force"))
                    scan = run_ai_agent_audit_scan(
                        settings,
                        get_db=get_db,
                        get_audit_db=get_audit_db,
                        actor=actor,
                        force=bool(force),
                        get_client_ip=get_client_ip,
                        get_ua=get_ua,
                        audit=audit,
                    )
                    payload = {"ok": True, "scan": scan}
                elif tool_name == "write_launch_preflight_execute":
                    status_code, payload = _execute_launch_preflight(actor, args, settings)
                elif tool_name == "write_share_create":
                    status_code, payload = _execute_share_create(actor, args)
                elif tool_name == "write_subtitle_upload":
                    status_code, payload = _execute_subtitle_upload(args)
                elif tool_name == "write_member_set_avatar_from_cloud":
                    status_code, payload = _execute_member_avatar_from_cloud(args)
                elif tool_name == "write_codex_handoff_create":
                    status_code, payload = _execute_codex_handoff_create(actor, args)
                else:
                    return json_resp({"ok": False, "msg": "DIRECT tool 尚未實作", "tool": tool_name}), 500
            else:
                path, body, msg = _build_write_tool_request(tool_name, spec, args)
                if msg:
                    _audit_agent_event("AI_AGENT_WRITE_TOOL", actor, success=False, detail=f"tool={tool_name},error={msg[:180]}")
                    return json_resp({"ok": False, "msg": msg}), 400
                if tool_name == "write_comfyui_generate":
                    body, msg = _prepare_comfyui_write_body(body)
                    if msg:
                        _audit_agent_event("AI_AGENT_WRITE_TOOL", actor, success=False, detail=f"tool={tool_name},error={msg[:180]}")
                        return json_resp({"ok": False, "msg": msg}), 400
                    if _should_run_official_workflow(body):
                        status_code, payload = _dispatch_official_comfyui_workflow(body)
                    else:
                        status_code, payload = _dispatch_internal_api(spec.get("method"), path, body)
                else:
                    status_code, payload = _dispatch_internal_api(spec.get("method"), path, body)
        except Exception as exc:
            audit(
                "AI_AGENT_WRITE_TOOL",
                get_client_ip(),
                user=_actor_value(actor, "username", "-"),
                ua=get_ua(),
                success=False,
                detail=f"tool={tool_name},error={str(exc)[:180]}",
            )
            return json_resp({"ok": False, "msg": str(exc), "tool": tool_name}), 502

        ok = 200 <= int(status_code or 500) < 400 and bool(payload.get("ok", True) if isinstance(payload, dict) else True)
        error_summary = _tool_payload_error_summary(payload) if not ok else ""
        audit(
            "AI_AGENT_WRITE_TOOL",
            get_client_ip(),
            user=_actor_value(actor, "username", "-"),
            ua=get_ua(),
            success=ok,
            detail=(
                f"tool={tool_name},status={status_code}"
                + (f",error={error_summary}" if error_summary else "")
            ),
        )
        return json_resp({
            "ok": ok,
            "tool": tool_name,
            "status": status_code,
            "elevated_once": bool(spec.get("write") and elevate_once and not write_enabled),
            "result": _safe_tool_payload(payload),
        }), (200 if ok else int(status_code or 500))

    @app.route("/api/ai-agent/readonly", methods=["GET"])
    @require_csrf_safe
    def ai_agent_readonly():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        scope = str(request.args.get("scope") or "all").strip().lower()
        if scope not in {"all", "resources", "server_mode", "comfyui", "remote_download", "jobs", "files", "storage", "member_mgmt", "attack_diag"}:
            return json_resp({"ok": False, "msg": "不支援的 scope"}, 400)
        limit = _coerce_limit(request.args.get("limit", "20"))
        actor_level = _actor_scope_payload(actor)
        payload = {
            "ok": True,
            "scope": scope,
            "actor": {
                "username": _actor_value(actor, "username", ""),
                "role": actor_level["role"],
            },
            "permissions": {
                "manage_members": actor_level["can_manage_members"],
                "manage_servers": actor_level["can_manage_servers"],
            },
        }
        if scope in {"all", "resources"}:
            payload["resources"] = _resource_snapshot()
        if scope in {"all", "server_mode"}:
            payload["server_mode"] = _server_mode_payload(actor)
        if scope in {"all", "jobs", "comfyui"}:
            payload["comfyui_jobs"] = _agent_list_comfyui_jobs(actor, limit=limit)
        if scope in {"all", "jobs", "remote_download"}:
            payload["remote_download_jobs"] = _agent_list_remote_download_jobs(actor, limit=limit)
        if scope in {"all", "files", "storage"}:
            payload["storage_files"] = _agent_list_storage_files(actor, limit=limit)
        if actor_level["can_manage_members"] and scope in {"all", "member_mgmt"}:
            payload["member_management"] = _member_management_payload(actor, limit=limit)
        if actor_level["can_manage_servers"] and scope in {"all", "attack_diag"}:
            payload["attack_diagnosis"] = _attack_diagnosis_payload(actor, limit=limit)
        _audit_agent_event(
            "AI_AGENT_READONLY",
            actor,
            success=True,
            detail=f"scope={scope},limit={limit},role={actor_level['role']}",
        )
        return json_resp(payload)

    @app.route("/api/ai-agent/conversation", methods=["GET", "PUT", "DELETE"])
    @require_csrf
    def ai_agent_conversation_route():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        if not fernet:
            return json_resp({"ok": False, "msg": "AI Agent encrypted memory key is unavailable"}), 503
        user_id = int(_actor_value(actor, "id") or 0)
        binding = _conversation_binding()
        data = {}
        if request.method == "GET":
            conversation_id = _conversation_id(request.args.get("conversation_id") or request.args.get("session_id"))
        else:
            data, bad_request = _request_json_dict()
            if bad_request:
                return bad_request
            conversation_id = _conversation_id(data.get("conversation_id") or data.get("session_id"))
        conn = get_db()
        try:
            _ensure_ai_agent_conversation_schema(conn)
            if request.method == "GET":
                row = conn.execute(
                    """
                    SELECT payload_encrypted, updated_at
                    FROM ai_agent_conversations
                    WHERE owner_user_id=? AND session_binding=? AND conversation_id=?
                    """,
                    (user_id, binding, conversation_id),
                ).fetchone()
                if not row:
                    _audit_agent_event("AI_AGENT_CONVERSATION_LOAD", actor, success=True, detail="empty")
                    return json_resp({
                        "ok": True,
                        "conversation_id": conversation_id,
                        "encrypted": True,
                        "payload": {"sessionId": conversation_id, "messages": [], "habits": {}},
                    })
                payload = _decrypt_conversation_payload(_row_value(row, "payload_encrypted"))
                _audit_agent_event("AI_AGENT_CONVERSATION_LOAD", actor, success=True, detail=f"messages={len(payload.get('messages') or [])}")
                return json_resp({
                    "ok": True,
                    "conversation_id": conversation_id,
                    "updated_at": _row_value(row, "updated_at"),
                    "encrypted": True,
                    "payload": payload,
                })
            if request.method == "DELETE":
                conn.execute(
                    "DELETE FROM ai_agent_conversations WHERE owner_user_id=? AND session_binding=? AND conversation_id=?",
                    (user_id, binding, conversation_id),
                )
                conn.commit()
                _audit_agent_event("AI_AGENT_CONVERSATION_CLEAR", actor, success=True, detail=conversation_id)
                return json_resp({"ok": True, "conversation_id": conversation_id})

            payload = _sanitize_conversation_payload(data.get("payload") if isinstance(data.get("payload"), dict) else data)
            if not payload.get("sessionId"):
                payload["sessionId"] = conversation_id
            encrypted = _encrypt_conversation_payload(payload)
            now = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                """
                INSERT INTO ai_agent_conversations
                    (owner_user_id, session_binding, conversation_id, payload_encrypted, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_user_id, session_binding, conversation_id)
                DO UPDATE SET payload_encrypted=excluded.payload_encrypted, updated_at=excluded.updated_at
                """,
                (user_id, binding, conversation_id, encrypted, now, now),
            )
            conn.commit()
            _audit_agent_event("AI_AGENT_CONVERSATION_SAVE", actor, success=True, detail=f"messages={len(payload.get('messages') or [])}")
            return json_resp({"ok": True, "conversation_id": conversation_id, "encrypted": True, "updated_at": now})
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            _audit_agent_event("AI_AGENT_CONVERSATION_ERROR", actor, success=False, detail=str(exc)[:180])
            return json_resp({"ok": False, "msg": str(exc)}), 500
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @app.route("/api/ai-agent/conversation-history", methods=["GET"])
    @require_csrf_safe
    def ai_agent_conversation_history_route():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        if not _actor_is_super_admin(actor):
            _audit_agent_event("AI_AGENT_CONVERSATION_HISTORY_DENIED", actor, success=False, detail="root_only")
            return json_resp({"ok": False, "msg": "只有 root 可檢視 AI Agent 歷史對話"}), 403
        if not fernet:
            return json_resp({"ok": False, "msg": "AI Agent encrypted memory key is unavailable"}), 503
        limit = _coerce_limit(request.args.get("limit") or "30")
        owner_filter = request.args.get("owner_user_id")
        conversation_filter = request.args.get("conversation_id")
        session_filter = request.args.get("session_binding")
        read_full = _parse_bool(request.args.get("include_payload")) is True
        clauses = ["1=1"]
        params = []
        if owner_filter not in {None, ""}:
            try:
                owner_id = int(owner_filter)
            except Exception:
                return json_resp({"ok": False, "msg": "owner_user_id 必須是整數"}), 400
            clauses.append("c.owner_user_id=?")
            params.append(owner_id)
        if conversation_filter:
            clauses.append("c.conversation_id=?")
            params.append(_conversation_id(conversation_filter))
        if session_filter:
            clauses.append("c.session_binding=?")
            params.append(str(session_filter)[:80])
        params.append(limit)
        conn = get_db()
        try:
            _ensure_ai_agent_conversation_schema(conn)
            rows = conn.execute(
                f"""
                SELECT
                    c.owner_user_id,
                    COALESCE(u.username, '') AS owner_username,
                    c.session_binding,
                    c.conversation_id,
                    c.payload_encrypted,
                    c.created_at,
                    c.updated_at
                FROM ai_agent_conversations c
                LEFT JOIN users u ON u.id = c.owner_user_id
                WHERE {' AND '.join(clauses)}
                ORDER BY c.updated_at DESC, c.created_at DESC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
            conversations = [
                _conversation_history_row(row, include_payload=read_full)
                for row in rows
            ]
            _audit_agent_event(
                "AI_AGENT_CONVERSATION_HISTORY",
                actor,
                success=True,
                detail=f"count={len(conversations)},include_payload={read_full}",
            )
            return json_resp({
                "ok": True,
                "encrypted": True,
                "root_only": True,
                "conversations": conversations,
            })
        except Exception as exc:
            _audit_agent_event("AI_AGENT_CONVERSATION_HISTORY_ERROR", actor, success=False, detail=str(exc)[:180])
            return json_resp({"ok": False, "msg": str(exc)}), 500
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @app.route("/api/ai-agent/status", methods=["GET"])
    @require_csrf_safe
    def ai_agent_status():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        actor_scope = _actor_scope_payload(actor)
        settings = get_system_settings() or {}
        public = public_ai_agent_settings(settings, actor=actor)
        audit_status = public_ai_agent_audit_status(settings, include_scan=_actor_is_super_admin(actor))
        health = ai_agent_health(settings)
        capabilities = ai_agent_capabilities(settings) if health.get("ok") else {}
        _audit_agent_event(
            "AI_AGENT_STATUS",
            actor,
            success=bool(health.get("ok")),
            detail=f"provider={public.get('provider')},mode={public.get('operation_mode')},health_url={health.get('url') or ''},health_msg={str(health.get('msg') or '')[:120]}",
        )
        return json_resp({
            "ok": True,
            "settings": public,
            "audit": audit_status,
            "health": health,
            "capabilities": capabilities,
            "actor": {
                "username": _actor_value(actor, "username", ""),
                "role": actor_scope["role"],
                "scope": actor_scope,
            },
        })

    @app.route("/api/ai-agent/models", methods=["GET"])
    @require_csrf_safe
    def ai_agent_models_route():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        settings = get_system_settings() or {}
        try:
            models = ai_agent_models(settings)
        except AiAgentError as exc:
            _audit_agent_event("AI_AGENT_MODELS", actor, success=False, detail=f"status={exc.status or '-'},error={str(exc)[:180]}")
            return json_resp({
                "ok": False,
                "msg": str(exc),
                "status": exc.status,
                "payload": exc.payload,
                "models": {},
                "backend_unavailable": True,
            })
        models = filter_retired_ai_agent_models(models)
        model_count = len(models.get("data") or []) if isinstance(models, dict) else 0
        _audit_agent_event("AI_AGENT_MODELS", actor, success=True, detail=f"models={model_count}")
        return json_resp({"ok": True, "models": models})

    @app.route("/api/ai-agent/audit-scan", methods=["GET", "POST"])
    @require_csrf
    def ai_agent_audit_scan_route():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        if not _actor_is_super_admin(actor):
            _audit_agent_event("AI_AGENT_AUDIT_SCAN_DENIED", actor, success=False, detail="root_only")
            return json_resp({"ok": False, "msg": "只有最高管理者可執行 AI Agent 審計掃描"}), 403
        settings = get_system_settings() or {}
        force = _parse_bool(request.args.get("force")) if request.method == "GET" else _parse_bool(request.json.get("force")) if request.is_json else False
        if force is None:
            force = False
        try:
            scan = run_ai_agent_audit_scan(
                settings,
                get_db=get_db,
                get_audit_db=get_audit_db,
                actor=actor,
                force=force,
                get_client_ip=get_client_ip,
                get_ua=get_ua,
                audit=audit,
            )
        except Exception as exc:
            _audit_agent_event("AI_AGENT_AUDIT_SCAN", actor, success=False, detail=f"force={force},error={str(exc)[:180]}")
            return json_resp({"ok": False, "msg": str(exc)}), 502
        _audit_agent_event("AI_AGENT_AUDIT_SCAN", actor, success=True, detail=f"force={force}")
        return json_resp({"ok": True, "scan": scan})

    @app.route("/api/ai-agent/audit-status", methods=["GET"])
    @require_csrf
    def ai_agent_audit_status_route():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        if not _actor_is_super_admin(actor):
            _audit_agent_event("AI_AGENT_AUDIT_STATUS_DENIED", actor, success=False, detail="root_only")
            return json_resp({"ok": False, "msg": "只有最高管理者可檢視 AI Agent 審計狀態"}), 403
        settings = get_system_settings() or {}
        _audit_agent_event("AI_AGENT_AUDIT_STATUS", actor, success=True, detail="include_scan=true")
        return json_resp({"ok": True, "audit_status": public_ai_agent_audit_status(settings, include_scan=True)})

    @app.route("/api/ai-agent/chat", methods=["POST"])
    @require_csrf
    def ai_agent_chat_route():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400
        boundary_reason = _ai_agent_boundary_block_reason(_extract_ai_agent_user_text(data))
        if boundary_reason:
            _audit_agent_event("AI_AGENT_BOUNDARY_BLOCK", actor, success=False, detail=boundary_reason)
            return json_resp({
                "ok": False,
                "msg": "此要求會存取伺服器作業系統檔案系統，不屬於站內 AI Agent 工具範圍；請改用站內雲端硬碟、runtime 文件或已授權管理工具。",
                "blocked_by": "server_policy",
                "policy": boundary_reason,
            }), 403
        settings = get_system_settings() or {}
        user_id = _actor_value(actor, "id", 0)
        session_id = str(data.get("session_id") or "").strip()[:120]
        binding = _actor_session_binding()
        base_key = f"hackme:{user_id}:{session_id or 'default'}"
        session_key = f"hackme:{user_id}:{binding}:{session_id or 'default'}" if binding else base_key
        public_settings = public_ai_agent_settings(settings, actor=actor)
        route_timeout_seconds = max(5, min(610, int(public_settings.get("request_timeout_seconds") or 120) + 5))
        try:
            result, timed_out = _run_ai_agent_chat_with_timeout(
                route_timeout_seconds,
                settings=settings,
                messages=data.get("messages"),
                prompt=data.get("prompt") or "",
                image_data_url=data.get("image_data_url") or "",
                model=data.get("model") or "",
                session_key=session_key,
                actor=actor,
            )
            if timed_out:
                audit(
                    "AI_AGENT_CHAT",
                    get_client_ip(),
                    user=_actor_value(actor, "username", "-"),
                    ua=get_ua(),
                    success=False,
                    detail=f"status=504,error=route_timeout_{route_timeout_seconds}s,image={bool(data.get('image_data_url'))}",
                )
                return json_resp({
                    "ok": False,
                    "msg": f"AI Agent backend 請求逾時（{route_timeout_seconds} 秒），已停止等待。請稍後重試或檢查 cloud vision 模型服務。",
                    "status": 504,
                    "payload": {"route_timeout_seconds": route_timeout_seconds},
                }), 504
        except AiAgentError as exc:
            response_status = int(getattr(exc, "http_status", None) or 502)
            if response_status < 400 or response_status > 599:
                response_status = 502
            audit(
                "AI_AGENT_CHAT",
                get_client_ip(),
                user=_actor_value(actor, "username", "-"),
                ua=get_ua(),
                success=False,
                detail=f"status={exc.status or response_status or '-'},error={str(exc)[:180]}",
            )
            return json_resp({"ok": False, "msg": str(exc), "status": exc.status, "payload": exc.payload}), response_status

        if _is_mock_chat_reply(result.get("content", "")):
            _audit_agent_event("AI_AGENT_CHAT", actor, success=False, detail="mock_backend_reply")
            return json_resp({
                "ok": False,
                "msg": "AI Agent 後端仍回傳 mock 回覆，請確認 ai_agent_api_base_url 是否指向真實 AI Agent endpoint",
            }), 502

        audit(
            "AI_AGENT_CHAT",
            get_client_ip(),
            user=_actor_value(actor, "username", "-"),
            ua=get_ua(),
            success=True,
            detail=f"model={result.get('model') or ''},image={bool(data.get('image_data_url'))}",
        )
        return json_resp({
            "ok": True,
            "message": {"role": "assistant", "content": result.get("content") or ""},
            "model": result.get("model") or "",
            "usage": result.get("usage") or {},
        })
