import base64
import json
import os
import re
import urllib.error
import urllib.request

from flask import request

from services.platform.settings import DangerousChangeBlocked, FEATURE_FLAG_KEYS, enforce_dangerous_confirm
from services.platform.settings_metadata import (
    DANGEROUS_SETTINGS,
    SETTING_DETAILS,
    setting_groups_payload,
)
from services.platform.time_settings import COMMON_SERVER_TIMEZONES, normalize_server_timezone, server_time_payload
from services.server.backpressure import apply_backpressure_settings, backpressure_status
from services.security.captcha import normalize_captcha_mode
from services.storage.global_capacity import parse_global_capacity_limit_mb
from services.storage.migration import sync_storage_root_contents
from services.storage.paths import validate_storage_root
from services.security.upload_security import (
    ensure_upload_security_schema,
    get_cloud_drive_security_policy,
    update_cloud_drive_security_policy,
)
from services.users.member_levels import (
    DEFAULT_MEMBER_LEVEL_RULES,
    ensure_member_level_rules_schema,
    serialize_member_level_rule,
    update_member_level_rule,
)


def _dangerous_blocked_payload(exc):
    items = []
    for key, danger, transition in exc.risky:
        detail = SETTING_DETAILS.get(key, {})
        items.append({
            "key": key,
            "label": detail.get("label") or key,
            "transition": transition,
            "warning": danger.get("warning") or "",
        })
    labels = "、".join(item["label"] for item in items) or "高敏感設定"
    return {
        "ok": False,
        "msg": (
            f"以下設定屬於高敏感變更，需在請求中加上 dangerous_confirm 才會生效：{labels}"
        ),
        "error": "dangerous_change_blocked",
        "dangerous_changes": items,
    }


def register_system_admin_settings_routes(app, ctx):
    BASE_DIR = ctx["BASE_DIR"]
    CURRENT_SERVER_BIND_STATE = ctx["CURRENT_SERVER_BIND_STATE"]
    STORAGE_DIR = ctx["STORAGE_DIR"]

    get_current_user_ctx = ctx["get_current_user_ctx"]
    get_db = ctx["get_db"]
    get_feature_settings = ctx["get_feature_settings"]
    get_system_settings = ctx["get_system_settings"]
    json_resp = ctx["json_resp"]
    save_feature_settings = ctx["save_feature_settings"]
    save_settings = ctx["save_settings"]
    audit = ctx["audit"]
    get_client_ip = ctx["get_client_ip"]
    role_rank = ctx["role_rank"]
    require_csrf = ctx["require_csrf"]
    require_csrf_safe = ctx["require_csrf_safe"]
    require_root_actor = ctx["require_root_actor"]

    access_control_settings_payload = ctx["access_control_settings_payload"]
    audit_settings_changed = ctx["audit_settings_changed"]
    cloud_drive_storage_payload = ctx["cloud_drive_storage_payload"]
    feature_dependency_error_payload = ctx["feature_dependency_error_payload"]
    find_feature_dependency_violations = ctx["find_feature_dependency_violations"]
    generate_internal_test_token = ctx["generate_internal_test_token"]
    generate_maintenance_bypass_token = ctx["generate_maintenance_bypass_token"]
    hash_internal_test_token = ctx["hash_internal_test_token"]
    hash_maintenance_bypass_token = ctx["hash_maintenance_bypass_token"]
    maintenance_bypass_expires_at = ctx["maintenance_bypass_expires_at"]
    normalize_ip_whitelist_or_none = ctx["normalize_ip_whitelist_or_none"]
    parse_int_in_range = ctx["parse_int_in_range"]
    parse_strict_bool = ctx["parse_strict_bool"]
    server_bind_settings_payload = ctx["server_bind_settings_payload"]
    server_ssl_payload = ctx["server_ssl_payload"]
    validate_comfyui_api_host = ctx["validate_comfyui_api_host"]
    validate_comfyui_api_url = ctx["validate_comfyui_api_url"]
    validate_comfyui_diffusers_device = ctx["validate_comfyui_diffusers_device"]
    validate_comfyui_diffusers_device_map = ctx["validate_comfyui_diffusers_device_map"]
    validate_comfyui_diffusers_dtype = ctx["validate_comfyui_diffusers_dtype"]
    validate_comfyui_local_async_offload = ctx["validate_comfyui_local_async_offload"]
    validate_comfyui_local_attention_mode = ctx["validate_comfyui_local_attention_mode"]
    validate_comfyui_local_cache_lru = ctx["validate_comfyui_local_cache_lru"]
    validate_comfyui_local_cache_mode = ctx["validate_comfyui_local_cache_mode"]
    validate_comfyui_local_cuda_malloc = ctx["validate_comfyui_local_cuda_malloc"]
    validate_comfyui_local_precision = ctx["validate_comfyui_local_precision"]
    validate_comfyui_local_reserve_vram_gb = ctx["validate_comfyui_local_reserve_vram_gb"]
    validate_comfyui_local_text_encoder_dtype = ctx["validate_comfyui_local_text_encoder_dtype"]
    validate_comfyui_local_unet_dtype = ctx["validate_comfyui_local_unet_dtype"]
    validate_comfyui_local_upcast_attention = ctx["validate_comfyui_local_upcast_attention"]
    validate_comfyui_local_vae_dtype = ctx["validate_comfyui_local_vae_dtype"]
    validate_comfyui_local_vram_mode = ctx["validate_comfyui_local_vram_mode"]
    validate_comfyui_relative_script = ctx["validate_comfyui_relative_script"]
    validate_huggingface_api_token = ctx["validate_huggingface_api_token"]
    normalize_huggingface_repo_id = ctx["normalize_huggingface_repo_id"]
    validate_listen_host = ctx["validate_listen_host"]
    validate_listen_port = ctx["validate_listen_port"]
    is_hhmm = ctx["is_hhmm"]

    def public_settings_payload(settings):
        payload = dict(settings or {})
        try:
            payload["server_max_content_current_mb"] = max(0, int(app.config.get("MAX_CONTENT_LENGTH") or 0) // (1024 * 1024))
        except Exception:
            payload["server_max_content_current_mb"] = 0
        payload["server_max_content_env_override"] = bool(str(os.environ.get("HTML_LEARNING_MAX_CONTENT_MB") or "").strip())
        comfyui_account_key = str(payload.get("comfyui_account_api_key") or "").strip()
        payload["comfyui_account_api_key"] = ""
        payload["comfyui_account_api_key_configured"] = bool(comfyui_account_key)
        huggingface_token = str(payload.get("comfyui_huggingface_api_token") or "").strip()
        payload["comfyui_huggingface_api_token"] = ""
        payload["comfyui_huggingface_api_token_configured"] = bool(huggingface_token)
        transmission_password = str(payload.get("transmission_rpc_password") or "").strip()
        payload["transmission_rpc_password"] = ""
        payload["transmission_rpc_password_configured"] = bool(transmission_password)
        return payload

    def normalize_internal_test_token_features(value):
        if value in (None, "", []):
            return []
        if isinstance(value, str):
            raw_items = re.split(r"[\s,]+", value)
        elif isinstance(value, (list, tuple, set)):
            raw_items = value
        else:
            raise ValueError("allowed_features 必須是陣列或逗號分隔字串")

        normalized = []
        unknown = []
        feature_flags = set(FEATURE_FLAG_KEYS)
        for item in raw_items:
            key = str(item or "").strip()
            if not key:
                continue
            if not key.startswith("feature_"):
                key = f"feature_{key}"
            if key not in feature_flags and not key.endswith("_enabled"):
                enabled_key = f"{key}_enabled"
                if enabled_key in feature_flags:
                    key = enabled_key
            if key not in feature_flags:
                unknown.append(key)
                continue
            if key not in normalized:
                normalized.append(key)
        if unknown:
            raise ValueError(f"未知的功能範圍：{', '.join(unknown)}")
        return normalized

    def normalize_hls_variant_tokens(value, *, allow_empty=False):
        tokens = []
        for part in str(value or "").replace(";", ",").split(","):
            token = str(part or "").strip().lower()
            if not token:
                continue
            if token == "original":
                normalized = "original"
            else:
                match = re.fullmatch(r"q?(\d{3,4})p?", token)
                if not match:
                    raise ValueError("HLS 保留畫質只能是 original 或 360p/480p/720p/1080p 這類高度")
                height = int(match.group(1))
                if height < 144 or height > 4320:
                    raise ValueError("HLS 保留畫質高度必須介於 144p-4320p")
                normalized = f"{height}p"
            if normalized not in tokens:
                tokens.append(normalized)
        if not tokens and not allow_empty:
            raise ValueError("HLS 保留畫質不可為空")
        return ",".join(tokens)

    def normalize_video_hls_cold_cleanup_updates(data):
        int_ranges = {
            "video_hls_protect_days_after_upload": (0, 3650),
            "video_hls_warm_plays_30d_below": (1, 1_000_000),
            "video_hls_cold_plays_30d_below": (0, 1_000_000),
            "video_hls_cold_no_play_days": (1, 3650),
            "video_hls_very_cold_no_play_days": (1, 36500),
            "video_hls_rebuild_plays_24h": (1, 1_000_000),
            "video_hls_rebuild_plays_7d": (1, 1_000_000),
            "video_hls_cold_cleanup_max_assets_per_run": (1, 10000),
        }
        for key, (minimum, maximum) in int_ranges.items():
            if key not in data:
                continue
            value = parse_int_in_range(data.get(key), minimum, maximum)
            if value is None:
                return f"{key} 必須是 {minimum}-{maximum} 的整數"
            data[key] = value
        for key in ("video_hls_warm_keep_variants", "video_hls_mobile_floor_keep_variants"):
            if key not in data:
                continue
            try:
                data[key] = normalize_hls_variant_tokens(data.get(key))
            except ValueError as exc:
                return f"{key}: {exc}"
        return ""

    def normalize_backpressure_updates(data):
        if "server_backpressure_mode" in data:
            mode = str(data.get("server_backpressure_mode") or "auto").strip().lower()
            if mode not in {"auto", "manual", "off"}:
                return "server_backpressure_mode 必須是 auto、manual 或 off"
            data["server_backpressure_mode"] = mode
        int_ranges = {
            "server_backpressure_thread_capacity": (0, 256),
            "server_backpressure_normal_limit": (0, 2048),
            "server_backpressure_heavy_limit": (0, 256),
            "server_backpressure_root_limit": (0, 64),
            "server_backpressure_fast_lane_reserved": (0, 64),
            "server_backpressure_retry_after_seconds": (1, 60),
            "server_backpressure_refresh_seconds": (1, 60),
        }
        for key, (minimum, maximum) in int_ranges.items():
            if key not in data:
                continue
            try:
                value = int(data.get(key))
            except Exception:
                return f"{key} 必須是 {minimum}-{maximum} 的整數"
            if value < minimum or value > maximum:
                return f"{key} 必須是 {minimum}-{maximum} 的整數"
            data[key] = value
        return ""

    @app.route("/api/admin/settings", methods=["GET","PUT"])
    @require_csrf_safe
    def admin_settings():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if actor["username"] != "root":
            return json_resp({"ok":False,"msg":"只有最高管理者可修改系統參數"}), 403

        if request.method == "GET":
            settings = get_system_settings()
            return json_resp({
                "ok": True,
                "settings": public_settings_payload(settings),
                "server_bind": server_bind_settings_payload(
                    settings,
                    current_host=CURRENT_SERVER_BIND_STATE.get("host"),
                    current_port=CURRENT_SERVER_BIND_STATE.get("port"),
                ),
                "server_ssl": server_ssl_payload(settings),
                "cloud_drive_storage": cloud_drive_storage_payload(settings),
                "backpressure": backpressure_status(app),
                "server_time": server_time_payload(settings),
                "server_timezones": list(COMMON_SERVER_TIMEZONES),
            })

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400
        current_settings = get_system_settings()
        bool_keys = {
            key for key, value in (current_settings or {}).items()
            if isinstance(value, bool)
        }
        for key in bool_keys & set(data.keys()):
            parsed = parse_strict_bool(data.get(key))
            if parsed is None:
                return json_resp({"ok":False,"msg":f"{key} 必須是布林值 true/false"}), 400
            data[key] = parsed
        if "server_listen_host" in data:
            host = validate_listen_host(data.get("server_listen_host"), allow_empty=True)
            if host is None:
                return json_resp({"ok":False,"msg":"server_listen_host 必須是 IP、localhost，或留空沿用環境變數"}), 400
            data["server_listen_host"] = host
        if "server_listen_port" in data:
            port = validate_listen_port(data.get("server_listen_port"), allow_empty=True)
            if port is None:
                return json_resp({"ok":False,"msg":"server_listen_port 必須是 1-65535，或 0/空值沿用環境變數"}), 400
            data["server_listen_port"] = port
        if "server_timezone" in data:
            timezone_name = normalize_server_timezone(data.get("server_timezone"))
            if timezone_name is None:
                return json_resp({"ok":False,"msg":"server_timezone 必須是有效的 IANA 時區名稱，例如 UTC、Asia/Taipei 或 America/New_York"}), 400
            data["server_timezone"] = timezone_name
        if "system_resource_board_refresh_seconds" in data:
            refresh_seconds = parse_int_in_range(data.get("system_resource_board_refresh_seconds"), 1, 300)
            if refresh_seconds is None:
                return json_resp({"ok":False,"msg":"system_resource_board_refresh_seconds 必須是 1-300 秒"}), 400
            data["system_resource_board_refresh_seconds"] = refresh_seconds
        refresh_ranges = {
            "server_backpressure_traffic_refresh_seconds": (1, 300),
            "server_output_refresh_seconds": (1, 300),
            "security_test_job_poll_seconds": (1, 300),
            "job_center_refresh_seconds": (1, 300),
            "economy_dashboard_refresh_seconds": (5, 600),
            "trading_dashboard_refresh_seconds": (2, 300),
            "trading_live_price_refresh_seconds": (1, 60),
            "trading_reference_price_refresh_seconds": (1, 60),
            "trading_reference_chart_refresh_seconds": (2, 300),
            "comfyui_job_poll_seconds": (1, 60),
            "notification_poll_seconds": (5, 600),
            "game_invite_poll_active_seconds": (2, 300),
            "game_invite_poll_idle_seconds": (10, 600),
            "game_invite_poll_hidden_seconds": (30, 1800),
            "server_connection_monitor_seconds": (5, 300),
            "drive_dashboard_lazy_refresh_seconds": (1, 300),
        }
        for key, (minimum, maximum) in refresh_ranges.items():
            if key not in data:
                continue
            refresh_seconds = parse_int_in_range(data.get(key), minimum, maximum)
            if refresh_seconds is None:
                return json_resp({"ok":False,"msg":f"{key} 必須是 {minimum}-{maximum} 秒"}), 400
            data[key] = refresh_seconds
        if "comfyui_connection_mode" in data:
            mode = str(data.get("comfyui_connection_mode") or "").strip().lower()
            if mode not in {"local", "remote", "diffusers"}:
                return json_resp({"ok":False,"msg":"comfyui_connection_mode 必須是 local、remote 或 diffusers"}), 400
            data["comfyui_connection_mode"] = mode
        if "comfyui_remote_api_url" in data:
            api_url = validate_comfyui_api_url(data.get("comfyui_remote_api_url"), allow_blank=True)
            if api_url is None:
                return json_resp({"ok":False,"msg":"comfyui_remote_api_url 必須是 http(s)://host:port，不可包含帳密、路徑或參數"}), 400
            data["comfyui_remote_api_url"] = api_url
        if "comfyui_base_dir" in data:
            raw_base = str(data.get("comfyui_base_dir") or "").strip()
            if raw_base:
                try:
                    data["comfyui_base_dir"] = str(validate_storage_root(raw_base, base_dir=BASE_DIR, create=False))
                except ValueError as exc:
                    return json_resp({"ok":False,"msg":f"comfyui_base_dir 不安全或格式錯誤：{exc}"}), 400
            else:
                data["comfyui_base_dir"] = ""
        if "comfyui_local_start_script" in data:
            base_dir_for_script = data.get("comfyui_base_dir")
            if base_dir_for_script is None:
                base_dir_for_script = (get_system_settings() or {}).get("comfyui_base_dir")
            script = validate_comfyui_relative_script(
                data.get("comfyui_local_start_script"),
                base_dir=base_dir_for_script,
            )
            if script is None:
                return json_resp({"ok":False,"msg":"comfyui_local_start_script 必須在 ComfyUI 本地資料夾內，可填相對路徑或同資料夾下的絕對路徑"}), 400
            data["comfyui_local_start_script"] = script
        if "comfyui_api_host" in data:
            host = validate_comfyui_api_host(data.get("comfyui_api_host"))
            if host is None:
                return json_resp({"ok":False,"msg":"comfyui_api_host 必須是主機名稱或 IP，不可包含 http://、路徑、帳密或特殊字元"}), 400
            data["comfyui_api_host"] = host
        if "comfyui_api_port" in data:
            try:
                port = int(data.get("comfyui_api_port"))
            except Exception:
                return json_resp({"ok":False,"msg":"comfyui_api_port 必須是 1-65535"}), 400
            if port < 1 or port > 65535:
                return json_resp({"ok":False,"msg":"comfyui_api_port 必須是 1-65535"}), 400
            data["comfyui_api_port"] = port
        local_choice_validators = {
            "comfyui_local_vram_mode": (
                validate_comfyui_local_vram_mode,
                "comfyui_local_vram_mode 必須是 auto、gpu_only、highvram、normalvram、lowvram、novram 或 cpu",
            ),
            "comfyui_local_precision": (
                validate_comfyui_local_precision,
                "comfyui_local_precision 必須是 auto、force_fp16 或 force_fp32",
            ),
            "comfyui_local_unet_dtype": (
                validate_comfyui_local_unet_dtype,
                "comfyui_local_unet_dtype 必須是 auto、fp32、fp64、bf16、fp16、fp8_e4m3fn、fp8_e5m2 或 fp8_e8m0fnu",
            ),
            "comfyui_local_vae_dtype": (
                validate_comfyui_local_vae_dtype,
                "comfyui_local_vae_dtype 必須是 auto、fp16、fp32 或 bf16",
            ),
            "comfyui_local_text_encoder_dtype": (
                validate_comfyui_local_text_encoder_dtype,
                "comfyui_local_text_encoder_dtype 必須是 auto、fp8_e4m3fn、fp8_e5m2、fp16、fp32 或 bf16",
            ),
            "comfyui_local_attention_mode": (
                validate_comfyui_local_attention_mode,
                "comfyui_local_attention_mode 必須是 auto、split、quad、pytorch、sage、flash 或 disable_xformers",
            ),
            "comfyui_local_upcast_attention": (
                validate_comfyui_local_upcast_attention,
                "comfyui_local_upcast_attention 必須是 auto、force 或 dont",
            ),
            "comfyui_local_cuda_malloc": (
                validate_comfyui_local_cuda_malloc,
                "comfyui_local_cuda_malloc 必須是 auto、enable 或 disable",
            ),
            "comfyui_local_async_offload": (
                validate_comfyui_local_async_offload,
                "comfyui_local_async_offload 必須是 auto、enable 或 disable",
            ),
            "comfyui_local_cache_mode": (
                validate_comfyui_local_cache_mode,
                "comfyui_local_cache_mode 必須是 auto、ram、classic、lru 或 none",
            ),
        }
        for key, (validator, message) in local_choice_validators.items():
            if key not in data:
                continue
            value = validator(data.get(key))
            if value is None:
                return json_resp({"ok":False,"msg":message}), 400
            data[key] = value
        for key in ("comfyui_local_cpu_vae", "comfyui_local_disable_smart_memory", "comfyui_local_deterministic"):
            if key not in data:
                continue
            parsed = parse_strict_bool(data.get(key))
            if parsed is None:
                return json_resp({"ok":False,"msg":f"{key} 必須是布林值 true/false"}), 400
            data[key] = parsed
        if "comfyui_local_cache_lru" in data:
            cache_lru = validate_comfyui_local_cache_lru(data.get("comfyui_local_cache_lru"))
            if cache_lru is None:
                return json_resp({"ok":False,"msg":"comfyui_local_cache_lru 必須是 0-10000 的整數"}), 400
            data["comfyui_local_cache_lru"] = cache_lru
        if "comfyui_local_reserve_vram_gb" in data:
            reserve_vram = validate_comfyui_local_reserve_vram_gb(data.get("comfyui_local_reserve_vram_gb"))
            if reserve_vram is None:
                return json_resp({"ok":False,"msg":"comfyui_local_reserve_vram_gb 必須留空，或填 0-128 GB 的數字"}), 400
            data["comfyui_local_reserve_vram_gb"] = reserve_vram
        if "comfyui_civitai_api_key" in data:
            data["comfyui_civitai_api_key"] = str(data.get("comfyui_civitai_api_key") or "").strip()
        clear_comfyui_account_api_key = False
        if "comfyui_account_api_key_clear" in data:
            clear_key = parse_strict_bool(data.pop("comfyui_account_api_key_clear"))
            if clear_key is None:
                return json_resp({"ok":False,"msg":"comfyui_account_api_key_clear 必須是布林值 true/false"}), 400
            if clear_key:
                clear_comfyui_account_api_key = True
                data["comfyui_account_api_key"] = ""
        if "comfyui_account_api_key" in data:
            account_api_key = str(data.get("comfyui_account_api_key") or "").strip()
            if account_api_key:
                if len(account_api_key) > 512 or any(ch.isspace() for ch in account_api_key):
                    return json_resp({"ok":False,"msg":"comfyui_account_api_key 不可包含空白，且長度不可超過 512"}), 400
                data["comfyui_account_api_key"] = account_api_key
            elif not clear_comfyui_account_api_key:
                data.pop("comfyui_account_api_key", None)
        if "comfyui_diffusers_model_repo" in data:
            repo_id = normalize_huggingface_repo_id(data.get("comfyui_diffusers_model_repo"), allow_blank=True)
            if repo_id is None:
                return json_resp({"ok":False,"msg":"comfyui_diffusers_model_repo 必須是 Hugging Face repo id 或模型頁網址，例如 dhead/waiIllustriousSDXL_v150"}), 400
            data["comfyui_diffusers_model_repo"] = repo_id
        if "comfyui_huggingface_cache_root" in data:
            raw_cache_root = str(data.get("comfyui_huggingface_cache_root") or "").strip()
            if raw_cache_root:
                try:
                    data["comfyui_huggingface_cache_root"] = str(
                        validate_storage_root(raw_cache_root, base_dir=BASE_DIR, create=False)
                    )
                except ValueError as exc:
                    return json_resp({"ok":False,"msg":f"comfyui_huggingface_cache_root 不安全或格式錯誤：{exc}"}), 400
            else:
                data["comfyui_huggingface_cache_root"] = ""
        clear_huggingface_token = False
        if "comfyui_huggingface_api_token_clear" in data:
            clear_token = parse_strict_bool(data.pop("comfyui_huggingface_api_token_clear"))
            if clear_token is None:
                return json_resp({"ok":False,"msg":"comfyui_huggingface_api_token_clear 必須是布林值 true/false"}), 400
            if clear_token:
                clear_huggingface_token = True
                data["comfyui_huggingface_api_token"] = ""
        if "comfyui_huggingface_api_token" in data:
            token = validate_huggingface_api_token(data.get("comfyui_huggingface_api_token"), allow_blank=True)
            if token is None:
                return json_resp({"ok":False,"msg":"comfyui_huggingface_api_token 不可包含空白，且長度不可超過 2048"}), 400
            if token:
                data["comfyui_huggingface_api_token"] = token
            elif not clear_huggingface_token:
                data.pop("comfyui_huggingface_api_token", None)
        if "comfyui_diffusers_device" in data:
            device = validate_comfyui_diffusers_device(data.get("comfyui_diffusers_device"))
            if device is None:
                return json_resp({"ok":False,"msg":"comfyui_diffusers_device 必須是 auto、cpu、cuda 或 mps"}), 400
            data["comfyui_diffusers_device"] = device
        if "comfyui_diffusers_dtype" in data:
            dtype = validate_comfyui_diffusers_dtype(data.get("comfyui_diffusers_dtype"))
            if dtype is None:
                return json_resp({"ok":False,"msg":"comfyui_diffusers_dtype 必須是 auto、float16、bfloat16 或 float32"}), 400
            data["comfyui_diffusers_dtype"] = dtype
        if "comfyui_diffusers_device_map" in data:
            device_map = validate_comfyui_diffusers_device_map(data.get("comfyui_diffusers_device_map"))
            if device_map is None:
                return json_resp({"ok":False,"msg":"comfyui_diffusers_device_map 必須是 auto、disabled、cuda、balanced、balanced_low_0 或 sequential"}), 400
            data["comfyui_diffusers_device_map"] = device_map
        if "comfyui_allow_in_process_diffusers" in data:
            allow_diffusers = parse_strict_bool(data.get("comfyui_allow_in_process_diffusers"))
            if allow_diffusers is None:
                return json_resp({"ok":False,"msg":"comfyui_allow_in_process_diffusers 必須是布林值 true/false"}), 400
            data["comfyui_allow_in_process_diffusers"] = allow_diffusers
        if "comfyui_diffusers_low_cpu_mem_usage" in data:
            low_cpu_mem_usage = parse_strict_bool(data.get("comfyui_diffusers_low_cpu_mem_usage"))
            if low_cpu_mem_usage is None:
                return json_resp({"ok":False,"msg":"comfyui_diffusers_low_cpu_mem_usage 必須是布林值 true/false"}), 400
            data["comfyui_diffusers_low_cpu_mem_usage"] = low_cpu_mem_usage
        if "comfyui_diffusers_cuda_fallback_to_cpu" in data:
            cuda_fallback_to_cpu = parse_strict_bool(data.get("comfyui_diffusers_cuda_fallback_to_cpu"))
            if cuda_fallback_to_cpu is None:
                return json_resp({"ok":False,"msg":"comfyui_diffusers_cuda_fallback_to_cpu 必須是布林值 true/false"}), 400
            data["comfyui_diffusers_cuda_fallback_to_cpu"] = cuda_fallback_to_cpu
        if "comfyui_diffusers_keep_downloaded_models" in data:
            keep_downloaded_models = parse_strict_bool(data.get("comfyui_diffusers_keep_downloaded_models"))
            if keep_downloaded_models is None:
                return json_resp({"ok":False,"msg":"comfyui_diffusers_keep_downloaded_models 必須是布林值 true/false"}), 400
            data["comfyui_diffusers_keep_downloaded_models"] = keep_downloaded_models
        if "comfyui_diffusers_disable_xet" in data:
            disable_xet = parse_strict_bool(data.get("comfyui_diffusers_disable_xet"))
            if disable_xet is None:
                return json_resp({"ok":False,"msg":"comfyui_diffusers_disable_xet 必須是布林值 true/false"}), 400
            data["comfyui_diffusers_disable_xet"] = disable_xet
        if "comfyui_max_batch_size" in data:
            try:
                batch_size = int(data.get("comfyui_max_batch_size"))
            except Exception:
                return json_resp({"ok":False,"msg":"comfyui_max_batch_size 必須是 1-8"}), 400
            if batch_size < 1 or batch_size > 8:
                return json_resp({"ok":False,"msg":"comfyui_max_batch_size 必須是 1-8"}), 400
            data["comfyui_max_batch_size"] = batch_size
        for key in ("comfyui_default_width", "comfyui_default_height"):
            if key in data:
                try:
                    size = int(data.get(key))
                except Exception:
                    return json_resp({"ok":False,"msg":f"{key} 必須是 64-2048 且為 8 的倍數"}), 400
                if size < 64 or size > 2048 or size % 8 != 0:
                    return json_resp({"ok":False,"msg":f"{key} 必須是 64-2048 且為 8 的倍數"}), 400
                data[key] = size
        storage_migration = None
        if "cloud_drive_storage_root" in data:
            raw_root = str(data.get("cloud_drive_storage_root") or "").strip()
            if raw_root:
                try:
                    data["cloud_drive_storage_root"] = str(validate_storage_root(raw_root, base_dir=BASE_DIR, create=True))
                except ValueError as exc:
                    return json_resp({"ok":False,"msg":f"cloud_drive_storage_root 不安全或格式錯誤：{exc}"}), 400
                try:
                    current_root = str(validate_storage_root(STORAGE_DIR, base_dir=BASE_DIR, create=True))
                    if os.path.realpath(current_root) != os.path.realpath(data["cloud_drive_storage_root"]):
                        storage_migration = sync_storage_root_contents(current_root, data["cloud_drive_storage_root"])
                except ValueError as exc:
                    return json_resp({"ok":False,"msg":f"cloud_drive_storage_root 遷移被拒絕：{exc}"}), 400
                except OSError as exc:
                    return json_resp({"ok":False,"msg":f"cloud_drive_storage_root 遷移失敗，設定未寫入：{exc}"}), 500
            else:
                data["cloud_drive_storage_root"] = ""
        if "cloud_drive_global_capacity_limit_mb" in data:
            try:
                data["cloud_drive_global_capacity_limit_mb"] = parse_global_capacity_limit_mb(data.get("cloud_drive_global_capacity_limit_mb"))
            except ValueError as exc:
                return json_resp({"ok":False,"msg":str(exc)}), 400
        if "captcha_mode" in data:
            raw_mode = str(data.get("captcha_mode") or "").strip().lower()
            if raw_mode and normalize_captcha_mode(raw_mode) != raw_mode:
                return json_resp({"ok":False,"msg":"captcha_mode 必須是 none、math、image 或 turnstile"}), 400
            data["captcha_mode"] = normalize_captcha_mode(raw_mode)
        if "password_reset_mode" in data:
            reset_mode = str(data.get("password_reset_mode") or "").strip().lower()
            if reset_mode not in {"admin_review", "email_token"}:
                return json_resp({"ok":False,"msg":"password_reset_mode 必須是 admin_review 或 email_token"}), 400
            data["password_reset_mode"] = reset_mode
        if "session_idle_timeout_minutes" in data:
            idle_timeout_minutes = parse_int_in_range(data.get("session_idle_timeout_minutes") or 0, 0, 1440)
            if idle_timeout_minutes is None:
                return json_resp({"ok":False,"msg":"session_idle_timeout_minutes 必須是 0-1440 分鐘；0 代表停用"}), 400
            data["session_idle_timeout_minutes"] = idle_timeout_minutes
        backpressure_error = normalize_backpressure_updates(data)
        if backpressure_error:
            return json_resp({"ok":False,"msg":backpressure_error}), 400
        if "server_max_content_mb" in data:
            max_content_mb = parse_int_in_range(data.get("server_max_content_mb"), 128, 1_048_576)
            if max_content_mb is None:
                return json_resp({"ok":False,"msg":"server_max_content_mb 必須是 128-1048576 MB"}), 400
            data["server_max_content_mb"] = max_content_mb
        if "max_manager_seats" in data:
            seats = parse_int_in_range(data.get("max_manager_seats"), 0, 1000)
            if seats is None:
                return json_resp({"ok":False,"msg":"max_manager_seats 必須是 0-1000"}), 400
            data["max_manager_seats"] = seats
        if "points_admin_weekly_salary_weekday" in data:
            weekday = parse_int_in_range(data.get("points_admin_weekly_salary_weekday"), 1, 7)
            if weekday is None:
                return json_resp({"ok":False,"msg":"points_admin_weekly_salary_weekday 必須是 1-7"}), 400
            data["points_admin_weekly_salary_weekday"] = weekday
        if "points_admin_weekly_salary_time" in data:
            if not is_hhmm(data.get("points_admin_weekly_salary_time")):
                return json_resp({"ok":False,"msg":"points_admin_weekly_salary_time 必須是 HH:MM"}), 400
            data["points_admin_weekly_salary_time"] = str(data.get("points_admin_weekly_salary_time")).strip()
        if "captcha_ttl_seconds" in data:
            try:
                ttl_seconds = int(data.get("captcha_ttl_seconds"))
            except Exception:
                return json_resp({"ok":False,"msg":"captcha_ttl_seconds 必須是 60-3600 秒"}), 400
            if ttl_seconds < 60 or ttl_seconds > 3600:
                return json_resp({"ok":False,"msg":"captcha_ttl_seconds 必須是 60-3600 秒"}), 400
            data["captcha_ttl_seconds"] = ttl_seconds
        if "video_tip_fee_percent" in data:
            fee_percent = parse_int_in_range(data.get("video_tip_fee_percent"), 0, 100)
            if fee_percent is None:
                return json_resp({"ok":False,"msg":"video_tip_fee_percent 必須是 0-100"}), 400
            data["video_tip_fee_percent"] = fee_percent
        if "video_tip_min_points" in data:
            minimum_points = parse_int_in_range(data.get("video_tip_min_points"), 1, 1_000_000)
            if minimum_points is None:
                return json_resp({"ok":False,"msg":"video_tip_min_points 必須是 1-1000000"}), 400
            data["video_tip_min_points"] = minimum_points
        if "video_e2ee_derivative_heights" in data:
            allowed = []
            for part in str(data.get("video_e2ee_derivative_heights") or "").replace(";", ",").split(","):
                try:
                    height = int(str(part or "").strip().lower().replace("p", ""))
                except Exception:
                    continue
                if height in {360, 480, 720, 1080} and height not in allowed:
                    allowed.append(height)
            if not allowed:
                return json_resp({"ok":False,"msg":"video_e2ee_derivative_heights 至少要包含 360、480、720 或 1080 其中一種"}), 400
            data["video_e2ee_derivative_heights"] = ",".join(str(item) for item in allowed)
        hls_cold_error = normalize_video_hls_cold_cleanup_updates(data)
        if hls_cold_error:
            return json_resp({"ok":False,"msg":hls_cold_error}), 400
        if "security_log_tail_lines" in data:
            tail_lines = parse_int_in_range(data.get("security_log_tail_lines"), 1, 10_000)
            if tail_lines is None:
                return json_resp({"ok":False,"msg":"security_log_tail_lines 必須是 1-10000"}), 400
            data["security_log_tail_lines"] = tail_lines
        if "snapshot_daily_time" in data:
            if not is_hhmm(data.get("snapshot_daily_time")):
                return json_resp({"ok":False,"msg":"snapshot_daily_time 必須是 HH:MM"}), 400
            data["snapshot_daily_time"] = str(data.get("snapshot_daily_time")).strip()
        if "bt_download_backend" in data:
            backend = str(data.get("bt_download_backend") or "auto").strip().lower()
            if backend not in {"auto", "transmission", "aria2"}:
                return json_resp({"ok":False,"msg":"bt_download_backend 必須是 auto、transmission 或 aria2"}), 400
            data["bt_download_backend"] = backend
        if "transmission_rpc_url" in data:
            rpc_url = str(data.get("transmission_rpc_url") or "").strip()
            if rpc_url and not (rpc_url.startswith("http://") or rpc_url.startswith("https://")):
                return json_resp({"ok":False,"msg":"transmission_rpc_url 必須是 http(s) RPC URL 或留空"}), 400
            data["transmission_rpc_url"] = rpc_url
        if "transmission_rpc_username" in data:
            data["transmission_rpc_username"] = str(data.get("transmission_rpc_username") or "").strip()
        if "transmission_rpc_password" in data:
            password_value = str(data.get("transmission_rpc_password") or "").strip()
            if password_value:
                data["transmission_rpc_password"] = password_value
            else:
                data.pop("transmission_rpc_password", None)
        remote_download_ranges = {
            "remote_download_max_concurrent_global": (1, 64),
            "remote_download_max_concurrent_per_user": (1, 16),
        }
        for key, (minimum, maximum) in remote_download_ranges.items():
            if key not in data:
                continue
            value = parse_int_in_range(data.get(key), minimum, maximum)
            if value is None:
                return json_resp({"ok":False,"msg":f"{key} 必須是 {minimum}-{maximum} 的整數"}), 400
            data[key] = value
        if "storage_trash_retention_days" in data:
            try:
                retention_days = int(data.get("storage_trash_retention_days"))
            except Exception:
                return json_resp({"ok":False,"msg":"storage_trash_retention_days 必須是 0-3650"}), 400
            if retention_days < 0 or retention_days > 3650:
                return json_resp({"ok":False,"msg":"storage_trash_retention_days 必須是 0-3650"}), 400
            data["storage_trash_retention_days"] = retention_days
        if "storage_maintenance_daily_time" in data:
            if not re.fullmatch(r"\d{2}:\d{2}", str(data.get("storage_maintenance_daily_time") or "")):
                return json_resp({"ok":False,"msg":"storage_maintenance_daily_time 必須是 HH:MM"}), 400
        violations = find_feature_dependency_violations(current_settings, data)
        if violations:
            return json_resp(feature_dependency_error_payload(violations)), 400

        before_settings = dict(current_settings)
        try:
            enforce_dangerous_confirm(current_settings, data)
        except DangerousChangeBlocked as exc:
            return json_resp(_dangerous_blocked_payload(exc)), 400
        try:
            settings = save_settings(data)
        except ValueError as exc:
            if "requires" in str(exc):
                violations = find_feature_dependency_violations(current_settings, data)
                return json_resp(feature_dependency_error_payload(violations or [{"feature": "", "feature_label": "功能", "required": "", "required_label": "父功能"}])), 400
            raise
        if not settings:
            return json_resp({"ok":False,"msg":"沒有可寫入的設定欄位"}), 400
        if "server_max_content_mb" in settings:
            app.config["MAX_CONTENT_LENGTH"] = int(settings["server_max_content_mb"]) * 1024 * 1024
        backpressure_state = apply_backpressure_settings(app, get_system_settings())

        audit_settings_changed("SETTINGS_CHANGED", actor, before_settings, settings, scope="system_settings")
        return json_resp({
            "ok": True,
            "msg": "系統參數已更新",
            "storage_migration": storage_migration,
            "settings": public_settings_payload(get_system_settings()),
            "server_bind": server_bind_settings_payload(
                get_system_settings(),
                current_host=CURRENT_SERVER_BIND_STATE.get("host"),
                current_port=CURRENT_SERVER_BIND_STATE.get("port"),
            ),
            "server_ssl": server_ssl_payload(get_system_settings()),
            "cloud_drive_storage": cloud_drive_storage_payload(get_system_settings()),
            "backpressure": backpressure_status(app) if backpressure_state else {},
            "server_time": server_time_payload(get_system_settings()),
            "server_timezones": list(COMMON_SERVER_TIMEZONES),
        })


    def _transmission_rpc_probe(rpc_url, username="", password="", *, timeout=4):
        url = str(rpc_url or "").strip()
        if not url or not (url.startswith("http://") or url.startswith("https://")):
            return {"ok": False, "msg": "Transmission RPC URL 必須是 http(s) URL"}
        headers = {"Content-Type": "application/json"}
        username = str(username or "")
        password = str(password or "")
        if username or password:
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        payload = json.dumps({"method": "session-get"}).encode("utf-8")

        def call(extra_headers=None):
            merged = dict(headers)
            if extra_headers:
                merged.update(extra_headers)
            req = urllib.request.Request(url, data=payload, headers=merged, method="POST")
            return urllib.request.urlopen(req, timeout=timeout)

        try:
            try:
                resp = call()
            except urllib.error.HTTPError as exc:
                if exc.code != 409:
                    if exc.code in {401, 403}:
                        return {"ok": False, "msg": "Transmission RPC 認證失敗，請檢查帳號密碼"}
                    return {"ok": False, "msg": f"Transmission RPC HTTP {exc.code}"}
                session_id = exc.headers.get("X-Transmission-Session-Id") or ""
                if not session_id:
                    return {"ok": False, "msg": "Transmission RPC 未回傳 session id"}
                resp = call({"X-Transmission-Session-Id": session_id})
            with resp:
                body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body or "{}")
            if data.get("result") != "success":
                return {"ok": False, "msg": f"Transmission RPC 回應失敗：{data.get('result') or 'unknown'}"}
            args = data.get("arguments") if isinstance(data.get("arguments"), dict) else {}
            version = str(args.get("version") or "").strip()
            return {"ok": True, "msg": "Transmission RPC 連線成功", "version": version}
        except TimeoutError:
            return {"ok": False, "msg": "Transmission RPC 連線逾時"}
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            return {"ok": False, "msg": f"Transmission RPC 連線失敗：{reason}"}
        except Exception as exc:
            return {"ok": False, "msg": f"Transmission RPC 測試失敗：{exc}"}

    @app.route("/api/admin/settings/transmission/test", methods=["POST"])
    @require_csrf_safe
    def admin_settings_transmission_test():
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True) or {}
        except Exception:
            return json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}), 400
        settings = get_system_settings()
        rpc_url = str(data.get("transmission_rpc_url") or settings.get("transmission_rpc_url") or "").strip()
        username = str(data.get("transmission_rpc_username") or settings.get("transmission_rpc_username") or "").strip()
        password = str(data.get("transmission_rpc_password") or "").strip()
        if not password:
            password = str(settings.get("transmission_rpc_password") or "").strip()
        result = _transmission_rpc_probe(rpc_url, username, password)
        status = 200 if result.get("ok") else 400
        return json_resp(result), status

    @app.route("/api/root/backpressure", methods=["GET", "PUT", "POST"])
    @require_csrf_safe
    def root_backpressure_settings():
        actor, error = require_root_actor()
        if error:
            return error
        if request.method == "GET":
            settings = get_system_settings()
            return json_resp({
                "ok": True,
                "settings": {
                    key: settings.get(key)
                    for key in (
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
                    )
                },
                "backpressure": backpressure_status(app),
            })
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg":"請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg":"請求內容格式錯誤"}), 400
        allowed = {
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
        }
        updates = {key: data[key] for key in allowed if key in data}
        if not updates:
            return json_resp({"ok":False,"msg":"沒有可更新的 backpressure 參數"}), 400
        if "server_backpressure_enabled" in updates:
            parsed = parse_strict_bool(updates.get("server_backpressure_enabled"))
            if parsed is None:
                return json_resp({"ok":False,"msg":"server_backpressure_enabled 必須是布林值"}), 400
            updates["server_backpressure_enabled"] = parsed
        if "server_backpressure_root_priority_enabled" in updates:
            parsed = parse_strict_bool(updates.get("server_backpressure_root_priority_enabled"))
            if parsed is None:
                return json_resp({"ok":False,"msg":"server_backpressure_root_priority_enabled 必須是布林值"}), 400
            updates["server_backpressure_root_priority_enabled"] = parsed
        backpressure_error = normalize_backpressure_updates(updates)
        if backpressure_error:
            return json_resp({"ok":False,"msg":backpressure_error}), 400
        before_settings = dict(get_system_settings())
        settings = save_settings(updates)
        apply_backpressure_settings(app, get_system_settings())
        audit_settings_changed("BACKPRESSURE_SETTINGS_CHANGED", actor, before_settings, settings, scope="system_settings")
        return json_resp({
            "ok": True,
            "msg": "Backpressure 參數已更新並即時套用",
            "settings": public_settings_payload(get_system_settings()),
            "backpressure": backpressure_status(app),
        })

    @app.route("/api/admin/settings/metadata", methods=["GET"])
    @require_csrf_safe
    def admin_settings_metadata():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok": False, "msg": "未登入"}), 401
        if actor["username"] != "root":
            return json_resp({"ok": False, "msg": "只有最高管理者可讀取系統參數元資料"}), 403
        return json_resp({
            "ok": True,
            "groups": setting_groups_payload(),
            "dangerous_keys": [
                {
                    "key": key,
                    "side": danger["side"],
                    "warning": danger["warning"],
                    "label": SETTING_DETAILS.get(key, {}).get("label") or key,
                }
                for key, danger in DANGEROUS_SETTINGS.items()
            ],
        })

    @app.route("/api/admin/cloud-drive/security-policy", methods=["GET", "PUT"])
    @require_csrf_safe
    def admin_cloud_drive_security_policy():
        actor, error = require_root_actor()
        if error:
            return error
        conn = get_db()
        try:
            ensure_upload_security_schema(conn)
            if request.method == "GET":
                return json_resp({"ok":True,"policy":get_cloud_drive_security_policy(conn)})
            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
            policy, err = update_cloud_drive_security_policy(conn, data)
            if err:
                return json_resp({"ok":False,"msg":err}), 400
            conn.commit()
            audit("CLOUD_DRIVE_POLICY_UPDATED", get_client_ip(), user=actor["username"], success=True, detail=str(policy))
            return json_resp({"ok":True,"msg":"雲端硬碟安全政策已更新","policy":policy})
        finally:
            conn.close()

    @app.route("/api/admin/features", methods=["GET", "PUT"])
    @require_csrf_safe
    def admin_features():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if actor["username"] != "root":
            return json_resp({"ok":False,"msg":"只有 root 可修改功能開關"}), 403

        if request.method == "GET":
            return json_resp({"ok":True,"features":get_feature_settings()})

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400

        before_settings = get_system_settings()
        violations = find_feature_dependency_violations(before_settings, data)
        if violations:
            return json_resp(feature_dependency_error_payload(violations)), 400
        # admin_features only accepts feature_* flags; restrict the dangerous
        # gate to those so extra keys (which save_feature_settings will strip)
        # do not produce confusing errors here. The /api/admin/settings PUT
        # path still guards every other key in its own enforcement call.
        feature_only_data = {
            key: value for key, value in data.items()
            if key.startswith("feature_") or key == "dangerous_confirm"
        }
        try:
            enforce_dangerous_confirm(before_settings, feature_only_data)
        except DangerousChangeBlocked as exc:
            return json_resp(_dangerous_blocked_payload(exc)), 400
        try:
            updates = save_feature_settings(data)
        except ValueError as exc:
            if "requires" in str(exc):
                violations = find_feature_dependency_violations(before_settings, data)
                return json_resp(feature_dependency_error_payload(violations or [{"feature": "", "feature_label": "功能", "required": "", "required_label": "父功能"}])), 400
            raise
        if not updates:
            return json_resp({"ok":False,"msg":"沒有可寫入的功能開關"}), 400
        audit_settings_changed("FEATURE_FLAGS_CHANGED", actor, before_settings, updates, scope="feature_flags")
        return json_resp({"ok":True,"msg":"功能開關已更新","features":updates})

    @app.route("/api/admin/access-controls", methods=["GET", "PUT"])
    @require_csrf_safe
    def admin_access_controls():
        actor, error = require_root_actor()
        if error:
            return error
        if request.method == "GET":
            return json_resp({"ok":True,"access_controls":access_control_settings_payload(get_system_settings())})
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400
        before_settings = get_system_settings()
        updates = {}
        for key in ("root_ip_whitelist_enabled", "root_ip_whitelist", "browser_only_mode_enabled"):
            if key in data:
                updates[key] = data[key]
        if "root_ip_whitelist" in updates:
            normalized_whitelist, bad_entries = normalize_ip_whitelist_or_none(updates["root_ip_whitelist"])
            if bad_entries:
                return json_resp({"ok":False,"msg":f"無效的 IP / CIDR：{', '.join(bad_entries)}"}), 400
            updates["root_ip_whitelist"] = normalized_whitelist
        if parse_strict_bool(updates.get("root_ip_whitelist_enabled")) and not str(updates.get("root_ip_whitelist") or before_settings.get("root_ip_whitelist") or "").strip():
            return json_resp({"ok":False,"msg":"啟用 root IP 白名單前，至少要填入一個有效的 IP 或 CIDR"}), 400
        if "clear_maintenance_bypass_token" in data and data.get("clear_maintenance_bypass_token"):
            updates["maintenance_bypass_token_hash"] = ""
            updates["maintenance_bypass_token_expires_at"] = ""
        if "clear_internal_test_token" in data and data.get("clear_internal_test_token"):
            updates["internal_test_login_token_hash"] = ""
            updates["internal_test_login_token_expires_at"] = ""
            updates["internal_test_login_token_user_id"] = 0
            updates["internal_test_login_token_username"] = ""
            updates["internal_test_login_token_allowed_features_json"] = "[]"
        if not updates:
            return json_resp({"ok":False,"msg":"沒有可寫入的存取控制設定"}), 400
        saved = save_settings(updates)
        audit_settings_changed("ACCESS_CONTROLS_CHANGED", actor, before_settings, saved, scope="access_controls")
        return json_resp({"ok":True,"msg":"存取控制設定已更新","access_controls":access_control_settings_payload(get_system_settings())})

    @app.route("/api/admin/access-controls/maintenance-bypass-token", methods=["POST"])
    @require_csrf
    def admin_rotate_maintenance_bypass_token():
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True) if request.is_json else {}
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400
        if data.get("confirm") != "ROTATE":
            return json_resp({"ok":False,"msg":"confirm 必須等於 ROTATE"}), 400
        ttl_minutes = data.get("ttl_minutes", 30)
        try:
            ttl_minutes = max(1, min(int(ttl_minutes), 24 * 60))
        except Exception:
            ttl_minutes = 30
        issued_value = generate_maintenance_bypass_token()
        expires_at = maintenance_bypass_expires_at(ttl_minutes)
        before_settings = get_system_settings()
        saved = save_settings({
            "maintenance_bypass_token_hash": hash_maintenance_bypass_token(issued_value),
            "maintenance_bypass_token_expires_at": expires_at,
        })
        audit_settings_changed(
            "MAINTENANCE_BYPASS_TOKEN_ROTATED",
            actor,
            before_settings,
            saved,
            scope="maintenance_bypass_token",
            extra={"ttl_minutes": ttl_minutes, "expires_at": expires_at},
        )
        return json_resp({
            "ok": True,
            "msg": "maintenance bypass token 已更新，token 只會顯示這一次",
            "token": issued_value,
            "expires_at": expires_at,
            "ttl_minutes": ttl_minutes,
            "access_controls": access_control_settings_payload(get_system_settings()),
        })

    @app.route("/api/admin/access-controls/internal-test-token", methods=["POST"])
    @require_csrf
    def admin_rotate_internal_test_token():
        actor, error = require_root_actor()
        if error:
            return error
        try:
            data = request.get_json(force=True) if request.is_json else {}
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400
        if data.get("confirm") != "ROTATE_INTERNAL_TEST_TOKEN":
            return json_resp({"ok":False,"msg":"confirm 必須等於 ROTATE_INTERNAL_TEST_TOKEN"}), 400
        ttl_minutes = data.get("ttl_minutes", 24 * 60)
        try:
            ttl_minutes = max(5, min(int(ttl_minutes), 30 * 24 * 60))
        except Exception:
            ttl_minutes = 24 * 60
        target_user_id = data.get("target_user_id")
        target_username = str(data.get("target_username") or "").strip()
        resolved_user = None
        conn = get_db()
        try:
            if target_user_id not in (None, ""):
                try:
                    resolved_user = conn.execute(
                        "SELECT id, username FROM users WHERE id=? LIMIT 1",
                        (int(target_user_id),),
                    ).fetchone()
                except Exception:
                    resolved_user = None
            if not resolved_user and target_username:
                resolved_user = conn.execute(
                    "SELECT id, username FROM users WHERE LOWER(username)=LOWER(?) LIMIT 1",
                    (target_username,),
                ).fetchone()
        finally:
            conn.close()
        if not resolved_user:
            return json_resp({"ok":False,"msg":"請指定存在的綁定帳號（target_user_id 或 target_username）"}), 400
        try:
            allowed_features = normalize_internal_test_token_features(data.get("allowed_features"))
        except ValueError as exc:
            return json_resp({"ok":False,"msg":str(exc)}), 400
        issued_value = generate_internal_test_token()
        expires_at = maintenance_bypass_expires_at(ttl_minutes)
        before_settings = get_system_settings()
        saved = save_settings({
            "internal_test_login_token_hash": hash_internal_test_token(issued_value),
            "internal_test_login_token_expires_at": expires_at,
            "internal_test_login_token_user_id": int(resolved_user["id"]),
            "internal_test_login_token_username": str(resolved_user["username"] or "").strip(),
            "internal_test_login_token_allowed_features_json": json.dumps(allowed_features, ensure_ascii=True, sort_keys=True),
        })
        audit_settings_changed(
            "INTERNAL_TEST_TOKEN_ROTATED",
            actor,
            before_settings,
            saved,
            scope="internal_test_token",
            extra={
                "ttl_minutes": ttl_minutes,
                "expires_at": expires_at,
                "target_user_id": int(resolved_user["id"]),
                "target_username": str(resolved_user["username"] or "").strip(),
                "allowed_features": allowed_features,
            },
        )
        return json_resp({
            "ok": True,
            "msg": "內測登入 token 已更新，token 只會顯示這一次",
            "token": issued_value,
            "expires_at": expires_at,
            "ttl_minutes": ttl_minutes,
            "target_user_id": int(resolved_user["id"]),
            "target_username": str(resolved_user["username"] or "").strip(),
            "allowed_features": allowed_features,
            "access_controls": access_control_settings_payload(get_system_settings()),
        })

    @app.route("/api/admin/member-level-rules", methods=["GET"])
    @require_csrf_safe
    def admin_member_level_rules():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        role = "super_admin" if actor["username"] == "root" else actor.get("role", "user")
        if role_rank(role) < role_rank("manager"):
            return json_resp({"ok":False,"msg":"需要管理員權限"}), 403

        conn = get_db()
        try:
            ensure_member_level_rules_schema(conn)
            conn.commit()
            rows = conn.execute("SELECT * FROM member_level_rules").fetchall()
            by_level = {row["level"]: dict(row) for row in rows}
            rules = []
            for level in DEFAULT_MEMBER_LEVEL_RULES:
                row = by_level.get(level)
                if row:
                    rules.append(serialize_member_level_rule(row))
            return json_resp({"ok":True,"rules":rules})
        finally:
            conn.close()

    @app.route("/api/admin/member-level-rules/<level>", methods=["PUT"])
    @require_csrf_safe
    def admin_update_member_level_rule(level):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if actor["username"] != "root":
            return json_resp({"ok":False,"msg":"只有 root 可管理會員等級規則"}), 403

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400

        conn = get_db()
        try:
            rule, err = update_member_level_rule(conn, level, data)
            if err:
                return json_resp({"ok":False,"msg":err}), 400
            conn.commit()
            audit("MEMBER_LEVEL_RULE_UPDATED", get_client_ip(), user=actor["username"], success=True, detail=f"level={level}, rule={rule}")
            return json_resp({"ok":True,"msg":"會員等級規則已更新","rule":rule})
        finally:
            conn.close()
