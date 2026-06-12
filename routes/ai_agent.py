import json
from datetime import datetime
import hashlib
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
    audit = deps.get("audit", lambda *args, **kwargs: None)
    json_resp = deps["json_resp"]
    require_csrf_safe = deps["require_csrf_safe"]
    require_csrf = deps.get("require_csrf", require_csrf_safe)
    role_rank = deps.get("role_rank", lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0))

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
        text = re.sub(r"\.(?:safetensors|ckpt|pt|pth|bin|gguf)$", "", text)
        return re.sub(r"[^0-9a-z]+", "", text)

    def _comfyui_model_query_tokens(value):
        text = str(value or "").strip().lower().replace("\\", "/")
        text = text.rsplit("/", 1)[-1]
        text = re.sub(r"\.(?:safetensors|ckpt|pt|pth|bin|gguf)$", "", text)
        return [
            token
            for token in re.split(r"[^0-9a-z]+", text)
            if token and token not in {"model", "checkpoint", "ckpt", "safetensors"}
        ]

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
        return path, body, ""

    def _prepare_comfyui_write_body(body):
        next_body = dict(body or {})
        requested = str(
            next_body.get("model")
            or next_body.get("checkpoint")
            or next_body.get("checkpoint_name")
            or ""
        ).strip()
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
                json=body if method in {"POST", "PUT", "PATCH"} else None,
                headers=headers,
                environ_base={"hackme.internal_dispatch": "ai_agent_write_tool"},
            )
        payload = response.get_json(silent=True)
        if payload is None:
            payload = {"raw": response.get_data(as_text=True)[:4000]}
        return response.status_code, payload

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
        if spec.get("write") and not write_enabled:
            _audit_agent_event("AI_AGENT_WRITE_TOOL", actor, success=False, detail=f"tool={tool_name},error=operation_mode_not_write,mode={public.get('operation_mode')}")
            return json_resp({
                "ok": False,
                "msg": "寫入型工具必須先將 AI Agent operation mode 切換為 write",
                "operation_mode": public.get("operation_mode"),
            }), 409
        if spec.get("write") and data.get("confirm") not in {True, "EXECUTE", "execute"}:
            _audit_agent_event("AI_AGENT_WRITE_TOOL", actor, success=False, detail=f"tool={tool_name},error=missing_confirm")
            return json_resp({"ok": False, "msg": "寫入型工具需要 confirm=true 或 confirm=\"EXECUTE\""}), 400

        status_code = 200
        try:
            if spec.get("method") == "DIRECT" and tool_name == "audit_scan":
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
        audit(
            "AI_AGENT_WRITE_TOOL",
            get_client_ip(),
            user=_actor_value(actor, "username", "-"),
            ua=get_ua(),
            success=ok,
            detail=f"tool={tool_name},status={status_code}",
        )
        return json_resp({
            "ok": ok,
            "tool": tool_name,
            "status": status_code,
            "result": _safe_tool_payload(payload),
        }), (200 if ok else int(status_code or 500))

    @app.route("/api/ai-agent/readonly", methods=["GET"])
    @require_csrf_safe
    def ai_agent_readonly():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        scope = str(request.args.get("scope") or "all").strip().lower()
        if scope not in {"all", "resources", "comfyui", "remote_download", "jobs", "files", "storage", "member_mgmt", "attack_diag"}:
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
            return json_resp({"ok": False, "msg": str(exc), "status": exc.status, "payload": exc.payload}), 502
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
