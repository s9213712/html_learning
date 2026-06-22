"""Group / danger metadata for system settings.

This module sits next to ``services.platform.settings`` and provides:

- ``SETTING_GROUPS``: ordered groups so the admin frontend can render
  domain-by-domain instead of one giant flat list of 120 keys.
- ``SETTING_DETAILS``: per-key label + one-line description.
- ``DANGEROUS_SETTINGS``: keys whose change requires an explicit
  ``dangerous_confirm`` field in the PUT payload. Each entry includes the
  expected confirm phrase, the side it guards (``disable``/``enable``/
  ``either``), and the operator-facing warning.

The dependency rules already live in ``settings.py`` as
``FEATURE_DEPENDENCY_RULES``; this module deliberately does not duplicate
them so there is one canonical source for feature graphs.
"""

from __future__ import annotations

from services.platform.settings import (
    DEFAULT_SETTINGS,
    FEATURE_DEPENDENCY_RULES,
    FEATURE_FLAG_KEYS,
    FEATURE_SETTING_LABELS,
    MANAGEMENT_ONLY_FEATURE_FLAGS,
)


SETTING_GROUPS = (
    {
        "key": "security",
        "title": "安全與稽核",
        "description": "Audit chain、IP 封鎖、登入暴力防護、Integrity Guard。錯改可能讓站台無法偵測攻擊。",
        "settings": (
            "audit_chain_enabled",
            "audit_chain_reseal_required",
            "integrity_guard_enabled",
            "integrity_guard_strict_mode",
            "ip_blocking_enabled",
            "login_violation_enabled",
            "rate_limit_violation_enabled",
            "max_login_failures",
            "max_login_fails_for_violation",
            "block_duration_minutes",
            "login_autofill_block_enabled",
            "production_single_ip_account_lock_enabled",
            "production_single_account_ip_lock_enabled",
            "root_ip_whitelist_enabled",
            "root_ip_whitelist",
            "browser_only_mode_enabled",
        ),
    },
    {
        "key": "sessions_and_recovery",
        "title": "會話與帳號復原",
        "description": "Session TTL、idle timeout、密碼重設模式、CAPTCHA。",
        "settings": (
            "session_ttl_hours",
            "session_idle_timeout_minutes",
            "password_reset_mode",
            "allow_register",
            "require_email_verification",
            "captcha_mode",
            "captcha_ttl_seconds",
            "captcha_turnstile_site_key",
        ),
    },
    {
        "key": "accounts_and_points",
        "title": "帳號席位 / 積分發放",
        "description": "管理員席位上限、初始積分與管理員週薪排程。",
        "settings": (
            "max_manager_seats",
            "points_admin_weekly_salary_enabled",
            "points_admin_weekly_salary_weekday",
            "points_admin_weekly_salary_time",
            "points_admin_weekly_salary_award_on_login",
        ),
    },
    {
        "key": "maintenance",
        "title": "維護模式 / Bypass Token",
        "description": "維護模式會擋住所有人，bypass token 給內部回測用。",
        "settings": (
            "maintenance_mode",
            "maintenance_bypass_token_hash",
            "maintenance_bypass_token_expires_at",
            "internal_test_login_token_hash",
            "internal_test_login_token_expires_at",
            "internal_test_login_token_user_id",
            "internal_test_login_token_username",
            "internal_test_login_token_allowed_features_json",
        ),
    },
    {
        "key": "server_bind",
        "title": "Server 綁定 / SSL / 時間",
        "description": "Listen host/port、SSL 開關與應用層時區。Listen / SSL 改動後需要重啟 server。",
        "settings": (
            "server_listen_host",
            "server_listen_port",
            "server_ssl_enabled",
            "server_timezone",
        ),
    },
    {
        "key": "server_performance",
        "title": "Server 效能 / Backpressure",
        "description": "依硬體資源自動推算請求容量，或由 root 手動覆寫 normal/heavy/root priority/fast-lane 保留量。",
        "settings": (
            "server_backpressure_enabled",
            "server_backpressure_mode",
            "server_backpressure_thread_capacity",
            "server_backpressure_normal_limit",
            "server_backpressure_heavy_limit",
            "server_backpressure_root_priority_enabled",
            "server_backpressure_root_limit",
            "server_backpressure_fast_lane_reserved",
            "server_backpressure_retry_after_seconds",
            "server_backpressure_refresh_seconds",
            "server_backpressure_traffic_refresh_seconds",
            "server_output_refresh_seconds",
            "security_test_job_poll_seconds",
            "system_resource_board_refresh_seconds",
            "job_center_refresh_seconds",
            "economy_dashboard_refresh_seconds",
            "trading_dashboard_refresh_seconds",
            "trading_live_price_refresh_seconds",
            "trading_reference_price_refresh_seconds",
            "trading_reference_chart_refresh_seconds",
            "comfyui_job_poll_seconds",
            "notification_poll_seconds",
            "game_invite_poll_active_seconds",
            "game_invite_poll_idle_seconds",
            "game_invite_poll_hidden_seconds",
            "server_connection_monitor_seconds",
            "drive_dashboard_lazy_refresh_seconds",
            "server_max_content_mb",
        ),
    },
    {
        "key": "cloud_drive",
        "title": "雲端硬碟",
        "description": "Storage root、容量上限、傳輸速率分級。",
        "settings": (
            "cloud_drive_storage_root",
            "cloud_drive_global_capacity_limit_mb",
            "cloud_drive_transfer_limits_enabled",
            "cloud_drive_transfer_limits_json",
            "storage_trash_retention_days",
            "storage_maintenance_auto_enabled",
            "storage_maintenance_daily_time",
            "storage_maintenance_last_date",
        ),
    },
    {
        "key": "snapshot",
        "title": "Snapshot / 備援",
        "description": "每日自動 snapshot 設定。",
        "settings": (
            "snapshot_daily_auto_enabled",
            "snapshot_daily_time",
            "snapshot_daily_last_date",
        ),
    },
    {
        "key": "appearance",
        "title": "外觀 / 佈景",
        "description": "站台主色、字型、版面密度。改動只影響顯示。",
        "settings": tuple(key for key in DEFAULT_SETTINGS if key.startswith("site_")),
    },
    {
        "key": "module_access",
        "title": "模組存取最低角色",
        "description": "每個模組可以指定最低身份才能進入。",
        "settings": tuple(key for key in DEFAULT_SETTINGS if key.startswith("module_") and key.endswith("_min_role")),
    },
    {
        "key": "features_user_facing",
        "title": "功能總開關 — 使用者端",
        "description": "對使用者可見的模組總開關。受 FEATURE_DEPENDENCY_RULES 約束。",
        "settings": tuple(
            key for key in FEATURE_FLAG_KEYS
            if key not in MANAGEMENT_ONLY_FEATURE_FLAGS
        ),
    },
    {
        "key": "features_management",
        "title": "功能總開關 — 管理端",
        "description": "管理者操作面板總開關。關掉會讓對應管理頁面消失。",
        "settings": tuple(sorted(MANAGEMENT_ONLY_FEATURE_FLAGS)),
    },
    {
        "key": "comfyui",
        "title": "ComfyUI 連線",
        "description": "ComfyUI host、port、本地 / 遠端模式、API key。",
        "settings": tuple(key for key in DEFAULT_SETTINGS if key.startswith("comfyui_")),
    },
    {
        "key": "ai_agent",
        "title": "AI Agent / LLM",
        "description": "Hermes / OpenAI-compatible API 連線、模型、逾時與輸入限制。",
        "settings": tuple(key for key in DEFAULT_SETTINGS if key.startswith("ai_agent_")),
    },
    {
        "key": "video",
        "title": "影音 / 打賞 / E2EE 串流",
        "description": "影音打賞抽成、strict E2EE 本機省流量版本與快取配額政策。",
        "settings": (
            "video_tip_fee_percent",
            "video_tip_min_points",
            "video_e2ee_derivatives_enabled",
            "video_e2ee_derivative_heights",
            "video_e2ee_derivative_reject_larger_than_original",
            "video_e2ee_derivative_quota_exempt",
        ),
    },
    {
        "key": "security_thresholds",
        "title": "安全告警門檻",
        "description": "聊天檢舉、申覆、隔離檔案、未知加密檔案的警示門檻。",
        "settings": tuple(key for key in DEFAULT_SETTINGS if key.startswith("security_")),
    },
    {
        "key": "chat",
        "title": "聊天規則",
        "description": "聊天過濾規則與通知靜音類型。",
        "settings": (
            "chat_filter_rules_json",
            "notification_muted_types",
        ),
    },
)


SETTING_DETAILS = {
    "site_theme_mode": {
        "label": "站點快速色系",
        "description": "全站預設的淺色 / 夜色 / 自訂色盤模式；使用者仍可在個人外觀中覆寫自己的畫面。",
    },
    "server_timezone": {
        "label": "伺服器應用時區",
        "description": "root 設定的 IANA 時區，例如 UTC 或 Asia/Taipei；用於管理頁時間檢查與後續排程顯示，不會修改作業系統時區。",
    },
    "server_backpressure_enabled": {
        "label": "Backpressure 啟用",
        "description": "超載時快速回 503 server_busy，避免主 server 無界接受請求直到下線。",
    },
    "server_backpressure_mode": {
        "label": "Backpressure 模式",
        "description": "auto 依硬體與 worker threads 推算；manual 由 root 指定 normal/heavy/root/fast-lane；off 關閉。",
    },
    "server_backpressure_thread_capacity": {
        "label": "每 worker thread 容量",
        "description": "0 代表自動偵測；gunicorn gthread 建議填每個 worker 的 threads 數。",
    },
    "server_backpressure_normal_limit": {
        "label": "普通請求上限",
        "description": "manual 模式下每 worker 同時允許的普通請求數；0 代表自動。",
    },
    "server_backpressure_heavy_limit": {
        "label": "重型請求上限",
        "description": "manual 模式下每 worker 同時允許的上傳、下載、HLS、生圖等重型請求數；0 代表自動。",
    },
    "server_backpressure_root_priority_enabled": {
        "label": "Root 優先管理通道",
        "description": "流量高峰時，已驗證 root 的 root/admin API 使用獨立有界 gate，避免營運管理被一般流量或重型任務卡住。",
    },
    "server_backpressure_root_limit": {
        "label": "Root 優先請求上限",
        "description": "每 worker 同時允許的 root/admin 管理請求數；0 代表自動。此值也可在 auto 模式下覆寫。",
    },
    "server_backpressure_fast_lane_reserved": {
        "label": "Fast lane 保留量",
        "description": "manual 模式下每 worker 保留給健康檢查、狀態頁與快速拒絕的 thread 數；0 代表自動。",
    },
    "server_backpressure_retry_after_seconds": {
        "label": "忙碌重試秒數",
        "description": "server_busy 503 的 Retry-After 秒數。",
    },
    "server_backpressure_refresh_seconds": {
        "label": "跨 worker 設定刷新秒數",
        "description": "gunicorn 多 worker 下，每個 worker 重新讀取 root Backpressure 設定的最長間隔。",
    },
    "server_backpressure_traffic_refresh_seconds": {
        "label": "流量折線圖更新頻率",
        "description": "root 伺服器設定頁可見時才輪詢 backpressure 流量折線圖；預設每 4 秒。",
    },
    "server_output_refresh_seconds": {
        "label": "伺服器即時輸出更新頻率",
        "description": "root 安全 / server console 分頁可見時才輪詢即時輸出；預設每 3 秒。",
    },
    "security_test_job_poll_seconds": {
        "label": "安全測試任務更新頻率",
        "description": "安全測試任務執行中才輪詢狀態；預設每 3 秒。",
    },
    "system_resource_board_refresh_seconds": {
        "label": "系統資源看板更新頻率",
        "description": "root 系統環境頁可見時才輪詢 CPU/RAM/GPU/VRAM；預設每 5 秒，離開頁面即停止。",
    },
    "job_center_refresh_seconds": {
        "label": "任務中心更新頻率",
        "description": "任務中心頁面可見時才輪詢 job 狀態；預設每 3 秒。",
    },
    "economy_dashboard_refresh_seconds": {
        "label": "積分錢包 dashboard 更新頻率",
        "description": "積分錢包頁可見時自動刷新錢包 / 鏈狀態；預設每 30 秒。",
    },
    "trading_dashboard_refresh_seconds": {
        "label": "交易 dashboard 更新頻率",
        "description": "交易所頁可見時刷新帳戶、掛單、倉位與快照；預設每 5 秒，不影響 server background engine。",
    },
    "trading_live_price_refresh_seconds": {
        "label": "交易即時價格更新頻率",
        "description": "交易 / 積分頁可見時刷新目前市場 live price；預設每 2 秒。",
    },
    "trading_reference_price_refresh_seconds": {
        "label": "交易參考價更新頻率",
        "description": "交易所頁可見時刷新參考價格卡片；預設每 1 秒。",
    },
    "trading_reference_chart_refresh_seconds": {
        "label": "交易 K 線圖更新頻率",
        "description": "交易所頁可見時刷新 reference chart 最新 K 線；預設每 5 秒。",
    },
    "comfyui_job_poll_seconds": {
        "label": "ComfyUI 工作進度更新頻率",
        "description": "ComfyUI 產圖或 workflow 執行中才輪詢 job 進度；預設每 1 秒。",
    },
    "ai_agent_provider": {
        "label": "AI Agent Provider",
        "description": "目前支援 hermes 或 OpenAI-compatible API；預設 hermes。",
    },
    "ai_agent_api_base_url": {
        "label": "AI Agent API Base URL",
        "description": "Hermes API server / OpenAI-compatible endpoint，例如 http://127.0.0.1:8642/v1。",
    },
    "ai_agent_api_key": {
        "label": "AI Agent API Key",
        "description": "送往 AI Agent backend 的 Bearer token；回傳給前端時會遮蔽。",
    },
    "ai_agent_model": {
        "label": "AI Agent 模型",
        "description": "預設送往 /v1/chat/completions 的 model 名稱。",
    },
    "ai_agent_operation_mode": {
        "label": "AI Agent 運作模式",
        "description": "readonly（唯讀）、assist（協助）、write（執行寫入候選）或 audit（僅審計），可在四種邏輯邊界間切換。",
    },
    "ai_agent_allowed_models": {
        "label": "AI Agent 允許模型清單",
        "description": "指定可用模型清單（逗號分隔），非空值時 chat 僅允許這些模型。",
    },
    "ai_agent_allowed_tools": {
        "label": "AI Agent 可調用工具清單",
        "description": "指定可用工具名稱（逗號分隔）；空白時依角色套用預設工具，root 可用完整工具集合。",
    },
    "ai_agent_audit_interval_minutes": {
        "label": "AI Agent 審計間隔（分鐘）",
        "description": "兩次審計掃描之最小間隔；同一 token cache 期限約束。",
    },
    "ai_agent_request_timeout_seconds": {
        "label": "AI Agent 逾時秒數",
        "description": "等待 Hermes / OpenAI-compatible API 回應的最長時間。",
    },
    "ai_agent_max_prompt_chars": {
        "label": "AI Agent 文字上限",
        "description": "單次請求送往模型的最大字元數；長對話會自動保留最新上下文並裁掉最舊訊息。",
    },
    "ai_agent_audit_cpu_percent_threshold": {
        "label": "AI Agent 審計 CPU 閾值 (%)",
        "description": "CPU 佔用超過此值會納入異常。",
    },
    "ai_agent_audit_ram_percent_threshold": {
        "label": "AI Agent 審計 RAM 閾值 (%)",
        "description": "RAM 佔用超過此值會納入異常。",
    },
    "ai_agent_audit_disk_percent_threshold": {
        "label": "AI Agent 審計磁碟閾值 (%)",
        "description": "磁碟使用率超過此值會納入異常。",
    },
    "ai_agent_audit_ip_event_rate_threshold": {
        "label": "AI Agent 審計 IP 請求速率（每分鐘）",
        "description": "指定每分鐘每個 IP 可接受的 sec-audit 請求次數，超過會警示。",
    },
    "ai_agent_audit_ip_event_rate_window_minutes": {
        "label": "AI Agent 審計 IP 速率窗口（分鐘）",
        "description": "用於聚合 secure_audit 的 IP 請求速率分析窗口。",
    },
    "ai_agent_audit_security_event_rate_threshold": {
        "label": "AI Agent 審計安全事件速率（每分鐘）",
        "description": "指定每分鐘安全事件總量上限，超過會警示。",
    },
    "ai_agent_audit_security_event_rate_window_minutes": {
        "label": "AI Agent 審計安全事件窗口（分鐘）",
        "description": "用於安全事件聚合窗口。",
    },
    "ai_agent_audit_auto_block_suspect_ip": {
        "label": "AI Agent 審計啟用疑似 IP 自動封鎖",
        "description": "觸發 IP 異常時自動封鎖來源 IP。",
    },
    "ai_agent_audit_block_minutes": {
        "label": "AI Agent 自動封鎖持續分鐘",
        "description": "AI Agent 審計自動封鎖 IP 的持續時間。",
    },
    "ai_agent_audit_notify_root": {
        "label": "AI Agent 審計異常通知根帳號",
        "description": "審計發現異常時是否在結果中產生 root 通知訊息。",
    },
    "ai_agent_allow_image_input": {
        "label": "允許圖片理解",
        "description": "開啟後前端可送 data:image 到 multimodal chat completion；關閉時只允許 t2t。",
    },
    "ai_agent_allow_tool_runs": {
        "label": "允許工具型任務",
        "description": "預留給後續 Hermes runs / approvals 流程；MVP chat route 不會自動開工具任務。",
    },
    "ai_agent_persona": {
        "label": "AI Agent 個性",
        "description": "選擇回覆人格模板：簡潔客服導向 / 嚴謹流程助手 / 創意流程統籌。",
    },
    "ai_agent_task_site_guide": {
        "label": "啟用網站導覽任務",
        "description": "開啟後 Agent 可回答站內頁面導覽與功能操作指引。",
    },
    "ai_agent_task_troubleshoot": {
        "label": "啟用排錯任務",
        "description": "開啟後 Agent 可提供生圖 / 下載排錯的診斷步驟建議。",
    },
    "ai_agent_task_prompt": {
        "label": "啟用提示詞任務",
        "description": "開啟後 Agent 可提供生圖提示詞與參數建議。",
    },
    "notification_poll_seconds": {
        "label": "通知更新頻率",
        "description": "登入後背景檢查通知未讀數；預設每 60 秒，頁面隱藏時暫停。",
    },
    "game_invite_poll_active_seconds": {
        "label": "遊戲邀請 active 更新頻率",
        "description": "遊戲頁或邀請視窗開啟時檢查多人邀請；預設每 5 秒。",
    },
    "game_invite_poll_idle_seconds": {
        "label": "遊戲邀請 idle 更新頻率",
        "description": "使用者在線但不在遊戲頁時檢查多人邀請；預設每 60 秒。",
    },
    "game_invite_poll_hidden_seconds": {
        "label": "遊戲邀請背景頁更新頻率",
        "description": "瀏覽器分頁隱藏時檢查多人邀請；預設每 180 秒。",
    },
    "server_connection_monitor_seconds": {
        "label": "伺服器連線監測頻率",
        "description": "前端連線狀態燈檢查 /api/version 的頻率；預設每 15 秒。",
    },
    "drive_dashboard_lazy_refresh_seconds": {
        "label": "雲端硬碟 dashboard lazy refresh",
        "description": "重複進入雲端硬碟時，多久內可沿用已載入狀態並只恢復背景任務；預設 10 秒。",
    },
    "video_e2ee_derivatives_enabled": {
        "label": "允許 strict E2EE 本機省流量版本",
        "description": "開啟後，發布者瀏覽器可本機產生 720p/480p 等 encrypted derivatives；伺服器仍不得解密或轉檔。",
    },
    "video_e2ee_derivative_heights": {
        "label": "E2EE 省流量畫質清單",
        "description": "逗號分隔，允許 1080,720,480,360。前端可嘗試上傳，後端會依此白名單接受或拒絕。",
    },
    "video_e2ee_derivative_reject_larger_than_original": {
        "label": "拒收比原檔大的 E2EE 衍生版本",
        "description": "避免壓縮反而增加儲存與流量負擔；關閉後仍不會讓伺服器取得明文。",
    },
    "video_e2ee_derivative_quota_exempt": {
        "label": "E2EE 衍生版本不計入雲端硬碟配額",
        "description": "作為服務端串流快取處理；原檔仍計入使用者容量。",
    },
    "audit_chain_enabled": {
        "label": "Audit Chain 簽章鏈",
        "description": "啟用後 audit log 會用前一筆 hash 串成 Merkle 鏈，竄改會被偵測。",
    },
    "audit_chain_reseal_required": {
        "label": "Audit Chain Reseal 必須",
        "description": "重新啟用 audit chain 時是否必須先 reseal 舊資料。",
    },
    "integrity_guard_enabled": {
        "label": "Integrity Guard 檔案校驗",
        "description": "對 runtime 重要檔案做 SHA256 校驗，發現竄改會發告警。",
    },
    "integrity_guard_strict_mode": {
        "label": "Integrity Guard 嚴格模式",
        "description": "嚴格模式下偵測到不符會直接拒絕服務，非嚴格模式僅紀錄。",
    },
    "ip_blocking_enabled": {
        "label": "IP 自動封鎖",
        "description": "暴力嘗試達門檻後自動封 IP。",
    },
    "login_violation_enabled": {
        "label": "登入失敗違規累積",
        "description": "登入失敗會累積到 user 的違規分數。",
    },
    "rate_limit_violation_enabled": {
        "label": "Rate limit 違規累積",
        "description": "超 rate limit 的請求會累積到違規分數。",
    },
    "max_login_failures": {
        "label": "登入失敗鎖定門檻",
        "description": "連續登入失敗幾次後鎖定該帳號。",
    },
    "max_login_fails_for_violation": {
        "label": "登入失敗違規門檻",
        "description": "失敗幾次後該登入嘗試會被當作違規。",
    },
    "block_duration_minutes": {
        "label": "封鎖時長 (分鐘)",
        "description": "IP 或帳號被自動封鎖的分鐘數。",
    },
    "login_autofill_block_enabled": {
        "label": "禁止瀏覽器自動填入密碼",
        "description": "登入表單會避免被密碼管理員自動帶入。",
    },
    "max_manager_seats": {
        "label": "管理員席位上限",
        "description": "root 可調整一般 manager 帳號最多可同時存在幾席。",
    },
    "points_admin_weekly_salary_enabled": {
        "label": "管理員週薪發放",
        "description": "啟用後系統會依 root 設定的星期與時間發放管理員週薪。",
    },
    "points_admin_weekly_salary_weekday": {
        "label": "管理員週薪星期",
        "description": "1-7，依 ISO 星期：1 是星期一，7 是星期日。",
    },
    "points_admin_weekly_salary_time": {
        "label": "管理員週薪時間",
        "description": "HH:MM，依 root 設定的伺服器應用時區判斷。",
    },
    "points_admin_weekly_salary_award_on_login": {
        "label": "登入時補發管理員週薪",
        "description": "啟用後管理員登入時也會嘗試補發當週週薪；預設關閉，避免發放時機依賴登入。",
    },
    "production_single_ip_account_lock_enabled": {
        "label": "生產：單 IP 同時只允許單一帳號",
        "description": "防止同一 IP 同時登入多個帳號。",
    },
    "production_single_account_ip_lock_enabled": {
        "label": "生產：單帳號鎖定首登入 IP",
        "description": "帳號首次登入後綁定該 IP，其他 IP 需重新驗證。",
    },
    "root_ip_whitelist_enabled": {
        "label": "Root IP 白名單啟用",
        "description": "限制 root 帳號只能從白名單 IP 登入。",
    },
    "root_ip_whitelist": {
        "label": "Root IP 白名單清單",
        "description": "以逗號分隔的 IP 或 CIDR。",
    },
    "browser_only_mode_enabled": {
        "label": "僅允許瀏覽器 UA",
        "description": "拒絕非瀏覽器 User-Agent (curl / wget 等)。",
    },
    "session_ttl_hours": {
        "label": "Session 最長存活 (小時)",
        "description": "session 從建立起算的絕對 TTL。",
    },
    "session_idle_timeout_minutes": {
        "label": "Session idle timeout (分鐘)",
        "description": "session 連續閒置這麼久後自動失效。",
    },
    "password_reset_mode": {
        "label": "密碼重設模式",
        "description": "admin_review = 管理者批准；email_token = 收信驗證。",
    },
    "allow_register": {
        "label": "開放公開註冊",
        "description": "關閉後僅 admin 可建立新帳號。",
    },
    "require_email_verification": {
        "label": "註冊需 Email 驗證",
        "description": "註冊後需點信件連結才能登入。",
    },
    "captcha_mode": {
        "label": "CAPTCHA 模式",
        "description": "none / math / image / turnstile。",
    },
    "captcha_ttl_seconds": {
        "label": "CAPTCHA TTL (秒)",
        "description": "CAPTCHA token 的有效秒數，60-3600。",
    },
    "captcha_turnstile_site_key": {
        "label": "Turnstile site key",
        "description": "Cloudflare Turnstile 公開 site key。",
    },
    "maintenance_mode": {
        "label": "維護模式 (擋全站)",
        "description": "啟用後所有使用者都被引導到維護頁。",
    },
    "server_ssl_enabled": {
        "label": "Server 啟用 SSL/HTTPS",
        "description": "關閉會降為 HTTP plain。",
    },
    "server_max_content_mb": {
        "label": "單次 HTTP request / 上傳上限 (MB)",
        "description": "控制 Flask/Werkzeug MAX_CONTENT_LENGTH；啟動腳本可用 --max-content-mb 或 HTML_LEARNING_MAX_CONTENT_MB 覆寫。",
    },
    "cloud_drive_global_capacity_limit_mb": {
        "label": "全站雲端硬碟容量上限 (MB)",
        "description": "-1 = 不限制，0 = 全禁，其他正整數為硬性 MB 上限。",
    },
    "cloud_drive_transfer_limits_enabled": {
        "label": "雲端硬碟分級限速",
        "description": "依會員等級套用上傳 / 下載速率上限。",
    },
    "comfyui_huggingface_cache_root": {
        "label": "Hugging Face 快取根目錄",
        "description": "Diffusers/HF/GGUF 下載快取根目錄；實際 hub cache 會放在此目錄的 hub/ 底下。",
    },
    "storage_trash_retention_days": {
        "label": "回收桶保留天數",
        "description": "Trash 內檔案多少天後自動清。",
    },
    "snapshot_daily_auto_enabled": {
        "label": "每日自動 Snapshot",
        "description": "每天指定時間自動建立站台 snapshot。",
    },
    "snapshot_daily_time": {
        "label": "每日 Snapshot 時間",
        "description": "HH:MM 格式。",
    },
    "video_tip_fee_percent": {
        "label": "影音打賞抽成 %",
        "description": "0-100。對發佈者收的平台費。",
    },
    "video_tip_min_points": {
        "label": "影音打賞最低金額 (points)",
        "description": "1-1000000。",
    },
    "storage_maintenance_auto_enabled": {
        "label": "雲端硬碟自動維護",
        "description": "每日定時掃描 orphan/quota。",
    },
}

# Auto-fill labels for feature_* keys from FEATURE_SETTING_LABELS
for _key, _label in FEATURE_SETTING_LABELS.items():
    if _key not in SETTING_DETAILS:
        SETTING_DETAILS[_key] = {"label": _label, "description": ""}


# --- DANGEROUS SETTINGS ---
# These need an explicit ``dangerous_confirm`` field in the PUT payload that
# matches the setting key, otherwise the save is rejected with an explanatory
# error. The aim is "fast for safe edits, friction for risky edits".
#
# ``side``: "disable" => trip only when transitioning from True -> False
#           "enable"  => trip only when transitioning from False -> True
#           "either"  => any change to this key needs confirm
#           "value"   => any change at all (including value writes) needs confirm

DANGEROUS_SETTINGS = {
    "audit_chain_enabled": {
        "side": "disable",
        "warning": "關閉 audit chain 會讓 audit log 不再可被竄改偵測，這是最高敏感操作。",
    },
    "integrity_guard_enabled": {
        "side": "disable",
        "warning": "關閉 Integrity Guard 後檔案竄改將不會被自動偵測。",
    },
    "ip_blocking_enabled": {
        "side": "disable",
        "warning": "關閉 IP 封鎖後，暴力破解嘗試不會被自動擋下。",
    },
    "login_violation_enabled": {
        "side": "disable",
        "warning": "關閉登入違規累積後，重複失敗不會累積到帳號違規分數。",
    },
    "production_single_account_ip_lock_enabled": {
        "side": "disable",
        "warning": "關閉單帳號 IP 綁定，帳號可被異地直接登入。",
    },
    "production_single_ip_account_lock_enabled": {
        "side": "disable",
        "warning": "關閉單 IP 單帳號限制，分享同一 IP 的帳號可同時上線。",
    },
    "server_ssl_enabled": {
        "side": "disable",
        "warning": "關閉 SSL/HTTPS 後流量改走 HTTP 明文，登入密碼會在網路上明傳。",
    },
    "allow_register": {
        "side": "enable",
        "warning": "開放公開註冊後，任何訪客都能建立帳號。生產環境通常應該關。",
    },
    "maintenance_mode": {
        "side": "enable",
        "warning": "啟用維護模式會立即擋下所有非 bypass 的使用者請求。",
    },
    "root_ip_whitelist_enabled": {
        "side": "enable",
        "warning": "啟用後若白名單清單不包含你目前的 IP，root 帳號會被鎖在外面。",
    },
    "browser_only_mode_enabled": {
        "side": "enable",
        "warning": "啟用後 curl / wget / 監控腳本都會被擋；只剩瀏覽器能存取。",
    },
}


def setting_detail(key):
    return SETTING_DETAILS.get(key, {"label": key, "description": ""})


def setting_groups_payload():
    """Return a serialisable copy of the group definitions for the API."""
    groups = []
    for group in SETTING_GROUPS:
        items = []
        for key in group["settings"]:
            detail = setting_detail(key)
            danger = DANGEROUS_SETTINGS.get(key)
            entry = {
                "key": key,
                "label": detail.get("label") or key,
                "description": detail.get("description") or "",
                "default": DEFAULT_SETTINGS.get(key),
            }
            if key in FEATURE_FLAG_KEYS:
                entry["is_feature_flag"] = True
                rule = FEATURE_DEPENDENCY_RULES.get(key, {})
                if rule:
                    entry["dependency_rule"] = {
                        "required": list(rule.get("required", ()) or ()),
                        "recommended": list(rule.get("recommended", ()) or ()),
                        "description": rule.get("description", ""),
                    }
            if danger:
                entry["dangerous"] = {
                    "side": danger["side"],
                    "warning": danger["warning"],
                }
            items.append(entry)
        groups.append({
            "key": group["key"],
            "title": group["title"],
            "description": group["description"],
            "settings": items,
        })
    # Also surface any DEFAULT_SETTINGS keys not yet placed into a group, so
    # the admin frontend can render them under a fallback bucket instead of
    # silently hiding them.
    placed = {key for group in SETTING_GROUPS for key in group["settings"]}
    misc = []
    for key in DEFAULT_SETTINGS:
        if key in placed:
            continue
        detail = setting_detail(key)
        misc.append({
            "key": key,
            "label": detail.get("label") or key,
            "description": detail.get("description") or "",
            "default": DEFAULT_SETTINGS.get(key),
        })
    if misc:
        groups.append({
            "key": "other",
            "title": "其他",
            "description": "尚未分群的設定。",
            "settings": misc,
        })
    return groups


def find_dangerous_changes(current_settings, updates):
    """Return ``[(key, danger_dict, transition)]`` for risky changes.

    ``transition`` is one of ``"enable"`` (False -> True),
    ``"disable"`` (True -> False), or ``"change"`` (non-bool value change).
    """
    if not isinstance(updates, dict):
        return []
    if not isinstance(current_settings, dict):
        current_settings = {}
    risky = []
    for key, danger in DANGEROUS_SETTINGS.items():
        if key not in updates:
            continue
        old = current_settings.get(key, DEFAULT_SETTINGS.get(key))
        new = updates[key]
        if isinstance(old, bool) or isinstance(new, bool):
            old_b = bool(old)
            new_b = bool(new)
            if old_b == new_b:
                continue
            transition = "enable" if (not old_b and new_b) else "disable"
            side = danger["side"]
            if side == "either" or side == transition or side == "value":
                risky.append((key, danger, transition))
        else:
            if old == new:
                continue
            if danger["side"] in ("either", "value"):
                risky.append((key, danger, "change"))
    return risky
