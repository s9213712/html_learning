import json
from datetime import datetime
import hashlib
import io
import os
import re
import shutil
from urllib.parse import urlencode

from flask import request

from services.ai_agent.hermes import (
    AiAgentError,
    ai_agent_capabilities,
    ai_agent_chat,
    ai_agent_health,
    public_ai_agent_audit_status,
    run_ai_agent_audit_scan,
    ai_agent_models,
    _is_mock_chat_reply,
    public_ai_agent_settings,
)
from services.storage.catalog import create_share_link


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
            "prompt", "negative_prompt", "model", "checkpoint", "checkpoint_name", "width", "height",
            "steps", "cfg", "cfg_scale", "sampler", "scheduler", "seed", "batch_size",
            "workflow", "workflow_id", "official_workflow_id", "template_id", "lora",
            "loras", "vae", "vae_name", "timeout_seconds", "confirm_billing",
            "backend_url", "comfyui_backend_url",
        },
        "required": {"prompt"},
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
        "body_fields": {"url", "source_type", "privacy_mode", "virtual_path", "filename"},
        "required": {"url"},
        "write": True,
    },
    "write_remote_download_direct": {
        "label": "建立 Direct download",
        "description": "建立 Direct download 任務。",
        "method": "POST",
        "path": "/api/cloud-drive/remote-download/tasks",
        "path_params": {},
        "body_fields": {"url", "privacy_mode", "virtual_path", "filename"},
        "required": {"url"},
        "write": True,
    },
    "write_remote_download_bt": {
        "label": "建立 BT/magnet download",
        "description": "建立 magnet 或 .torrent URL 下載任務。",
        "method": "POST",
        "path": "/api/cloud-drive/remote-download/torrent-tasks",
        "path_params": {},
        "body_fields": {"url", "privacy_mode", "virtual_path", "filename"},
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
        "label": "重試任務",
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
        "label": "發布既有雲端影音",
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

    def _audit_agent_event(action, actor=None, *, success=True, detail=""):
        audit(
            action,
            get_client_ip(),
            user=_actor_value(actor, "username", "-"),
            ua=get_ua(),
            success=success,
            detail=str(detail or "")[:500],
        )

    def _write_tool_public_spec(name, spec):
        return {
            "name": name,
            "label": spec.get("label") or name,
            "description": spec.get("description") or "",
            "method": spec.get("method") if spec.get("method") != "DIRECT" else "POST",
            "required": sorted(spec.get("required") or []),
            "path_params": sorted((spec.get("path_params") or {}).keys()),
            "body_fields": sorted(spec.get("body_fields") or []),
            "query_fields": sorted(spec.get("query_fields") or []),
            "write": bool(spec.get("write")),
            "root_only": True,
            "requires_confirm": bool(spec.get("write")),
        }

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

    def _request_json_dict():
        try:
            data = request.get_json(force=True)
        except Exception:
            return None, (json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400)
        if not isinstance(data, dict):
            return None, (json_resp({"ok": False, "msg": "請求內容格式錯誤"}), 400)
        return data, None

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

    def _build_write_tool_request(tool_name, spec, args):
        missing = [
            key for key in sorted(spec.get("required") or [])
            if _is_missing_arg(args.get(key))
        ]
        if missing:
            return None, None, f"缺少必要參數：{', '.join(missing)}"

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
        if tool_name == "write_remote_download_direct":
            body["source_type"] = "direct"
        if tool_name == "write_cloud_drive_remote_download" and not str(body.get("source_type") or "").strip():
            body["source_type"] = "direct"
        return path, body, ""

    def _prepare_comfyui_write_body(body):
        next_body = dict(body or {})
        requested = str(
            next_body.get("model")
            or next_body.get("checkpoint")
            or next_body.get("checkpoint_name")
            or ""
        ).strip()
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

    @app.route("/api/ai-agent/write-tools", methods=["GET"])
    @require_csrf_safe
    def ai_agent_write_tools_route():
        actor, denied = _require_write_tool_actor()
        if denied:
            return denied
        settings = get_system_settings() or {}
        public = public_ai_agent_settings(settings, actor=actor)
        effective_names = _write_tool_effective_names(settings, actor)
        tools = [
            _write_tool_public_spec(name, spec)
            for name, spec in AI_AGENT_WRITE_TOOL_SPECS.items()
            if name in effective_names
        ]
        _audit_agent_event(
            "AI_AGENT_WRITE_TOOLS_LIST",
            actor,
            success=True,
            detail=f"mode={public.get('operation_mode')},tools={len(tools)}",
        )
        return json_resp({
            "ok": True,
            "root_only": True,
            "operation_mode": public.get("operation_mode"),
            "write_enabled": bool((public.get("operation_mode_policy") or {}).get("write_enabled")),
            "tools": tools,
        })

    @app.route("/api/ai-agent/write-tools/execute", methods=["POST"])
    @require_csrf
    def ai_agent_write_tool_execute_route():
        actor, denied = _require_write_tool_actor()
        if denied:
            return denied
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
                        actor=actor,
                        force=bool(force),
                        get_client_ip=get_client_ip,
                        get_ua=get_ua,
                        audit=audit,
                    )
                    payload = {"ok": True, "scan": scan}
                elif tool_name == "write_share_create":
                    status_code, payload = _execute_share_create(actor, args)
                elif tool_name == "write_subtitle_upload":
                    status_code, payload = _execute_subtitle_upload(args)
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
        settings = get_system_settings() or {}
        user_id = _actor_value(actor, "id", 0)
        session_id = str(data.get("session_id") or "").strip()[:120]
        binding = _actor_session_binding()
        base_key = f"hackme:{user_id}:{session_id or 'default'}"
        session_key = f"hackme:{user_id}:{binding}:{session_id or 'default'}" if binding else base_key
        try:
            result = ai_agent_chat(
                settings,
                messages=data.get("messages"),
                prompt=data.get("prompt") or "",
                image_data_url=data.get("image_data_url") or "",
                model=data.get("model") or "",
                session_key=session_key,
                actor=actor,
            )
        except AiAgentError as exc:
            audit(
                "AI_AGENT_CHAT",
                get_client_ip(),
                user=_actor_value(actor, "username", "-"),
                ua=get_ua(),
                success=False,
                detail=f"status={exc.status or '-'},error={str(exc)[:180]}",
            )
            return json_resp({"ok": False, "msg": str(exc), "status": exc.status, "payload": exc.payload}), 502

        if _is_mock_chat_reply(result.get("content", "")):
            _audit_agent_event("AI_AGENT_CHAT", actor, success=False, detail="mock_backend_reply")
            return json_resp({
                "ok": False,
                "msg": "AI Agent 後端仍回傳 mock 回覆，請確認 ai_agent_api_base_url 是否指向真實 Hermes endpoint",
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
