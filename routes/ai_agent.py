import json
from datetime import datetime
import os
import shutil

from flask import request

from services.ai_agent.hermes import (
    AiAgentError,
    ai_agent_capabilities,
    ai_agent_chat,
    ai_agent_health,
    ai_agent_models,
    public_ai_agent_settings,
)


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

    def _actor_or_401():
        actor = get_current_user_ctx()
        if not actor:
            return None, json_resp({"ok": False, "msg": "請先登入"}, 401)
        settings = get_system_settings() or {}
        min_role = str(settings.get("module_ai_agent_min_role") or "user")
        actor_role = _coerce_role(actor)
        if _actor_value(actor, "username") != "root" and role_rank(actor_role) < role_rank(min_role):
            return None, json_resp({"ok": False, "msg": "沒有 AI Agent 使用權限"}, 403)
        return actor, None

    @app.route("/api/ai-agent/readonly", methods=["GET"])
    @require_csrf_safe
    def ai_agent_readonly():
        actor, denied = _actor_or_401()
        if denied:
            return denied
        scope = str(request.args.get("scope") or "all").strip().lower()
        if scope not in {"all", "resources", "comfyui", "remote_download", "jobs", "member_mgmt", "attack_diag"}:
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
        if actor_level["can_manage_members"] and scope in {"all", "member_mgmt"}:
            payload["member_management"] = _member_management_payload(actor, limit=limit)
        if actor_level["can_manage_servers"] and scope in {"all", "attack_diag"}:
            payload["attack_diagnosis"] = _attack_diagnosis_payload(actor, limit=limit)
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
        health = ai_agent_health(settings)
        capabilities = ai_agent_capabilities(settings) if health.get("ok") else {}
        return json_resp({
            "ok": True,
            "settings": public,
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
        _actor, denied = _actor_or_401()
        if denied:
            return denied
        settings = get_system_settings() or {}
        try:
            models = ai_agent_models(settings)
        except AiAgentError as exc:
            return json_resp({"ok": False, "msg": str(exc), "status": exc.status, "payload": exc.payload}), 502
        return json_resp({"ok": True, "models": models})

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
        session_key = f"hackme:{user_id}:{session_id or 'default'}"
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
