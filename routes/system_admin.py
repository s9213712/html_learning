import ipaddress
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from flask import request, send_file

from routes.system_admin_sections import (
    register_system_admin_runtime_routes,
    register_system_admin_security_routes,
    register_system_admin_settings_routes,
)

from services.security.access_controls import (
    access_control_settings_payload,
    generate_internal_test_token,
    generate_maintenance_bypass_token,
    hash_internal_test_token,
    hash_maintenance_bypass_token,
    maintenance_bypass_expires_at,
)
from services.platform.bootstrap import CURRENT_SCHEMA_VERSION, get_schema_version
from services.system.integrity_guard import CONFIRM_APPROVE
from services.users.member_levels import (
    DEFAULT_MEMBER_LEVEL_RULES,
    ensure_member_level_rules_schema,
    serialize_member_level_rule,
    update_member_level_rule,
)
from services.system.notifications import create_root_notification_if_enabled
from services.server.bind import (
    server_bind_settings_payload,
    server_ssl_settings_payload,
    validate_listen_host,
    validate_listen_port,
)
from services.security.captcha import normalize_captcha_mode
from services.storage.paths import validate_storage_root
from services.storage.capacity_audit import audit_storage_capacity, storage_disk_usage
from services.storage.global_capacity import resolve_global_capacity_limit
from services.security.upload_security import (
    ensure_upload_security_schema,
    get_cloud_drive_security_policy,
    update_cloud_drive_security_policy,
)
from services.comfyui.settings import (
    normalize_comfyui_connection_mode,
    normalize_huggingface_repo_id,
    validate_comfyui_api_host,
    validate_comfyui_api_port,
    validate_comfyui_api_url,
    validate_comfyui_batch_size,
    validate_comfyui_diffusers_device,
    validate_comfyui_diffusers_device_map,
    validate_comfyui_diffusers_dtype,
    validate_comfyui_dimension,
    validate_comfyui_local_async_offload,
    validate_comfyui_local_attention_mode,
    validate_comfyui_local_cache_lru,
    validate_comfyui_local_cache_mode,
    validate_comfyui_local_cuda_malloc,
    validate_comfyui_local_precision,
    validate_comfyui_local_reserve_vram_gb,
    validate_comfyui_local_text_encoder_dtype,
    validate_comfyui_local_unet_dtype,
    validate_comfyui_local_upcast_attention,
    validate_comfyui_local_vae_dtype,
    validate_comfyui_local_vram_mode,
    validate_comfyui_relative_script,
    validate_huggingface_api_token,
    validate_huggingface_repo_id,
)
from services.platform.settings import find_feature_dependency_violations
from services.server.runtime import default_runtime_root


SECURITY_TEST_JOBS = {}
SECURITY_TEST_JOBS_LOCK = threading.Lock()


GIT_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,119}$")
SERVER_UPDATE_WARNING = "此更新直接來自 GitHub diff/merge，尚未經本機測試驗證；更新後請自行執行 smoke test、權限測試與 debug。"


def _parse_strict_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y", "t"}:
            return True
        if normalized in {"0", "false", "no", "off", "n", "f"}:
            return False
    return None


def _parse_int_in_range(value, minimum, maximum):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        parsed = int(value)
    elif isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        parsed = int(value.strip())
    else:
        return None
    if parsed < minimum or parsed > maximum:
        return None
    return parsed


def _is_hhmm(value):
    text = str(value or "").strip()
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text))


def _normalize_ip_whitelist_or_none(raw):
    entries = []
    bad = []
    for item in str(raw or "").replace("\n", ",").split(","):
        value = item.strip()
        if not value:
            continue
        try:
            if "/" in value:
                ipaddress.ip_network(value, strict=False)
            else:
                ipaddress.ip_address(value)
            entries.append(value)
        except ValueError:
            bad.append(value)
    if bad:
        return None, bad
    return ",".join(entries), []


def _feature_dependency_error_payload(violations):
    first = violations[0]
    missing_labels = "、".join(item["required_label"] for item in violations)
    return {
        "ok": False,
        "msg": f"{first['feature_label']} 需要先啟用：{missing_labels}",
        "violations": violations,
    }


def public_relative_path(path, base_dir):
    if not path:
        return "-"
    try:
        base = os.path.abspath(base_dir)
        target = os.path.abspath(path) if os.path.isabs(path) else os.path.abspath(os.path.join(base, path))
        rel = os.path.relpath(target, base)
        if rel == ".":
            return "."
        if rel.startswith(".."):
            return f"<outside>/{os.path.basename(target) or 'path'}"
        return rel.replace("\\", "/")
    except Exception:
        return os.path.basename(str(path)) or "-"


def validate_git_branch_name(value):
    branch = str(value or "").strip()
    if not branch or branch in {"HEAD", ".", ".."}:
        return None
    if branch.startswith("/") or branch.endswith("/") or ".." in branch or branch.endswith(".lock"):
        return None
    if not GIT_BRANCH_RE.match(branch):
        return None
    return branch


def restart_launcher_code():
    return r"""
import os
import socket
import subprocess
import sys
import time

python_exe, script_path, base_dir, parent_pid, host, port = sys.argv[1:7]
parent_pid = int(parent_pid)
port = int(port)

def parent_alive():
    try:
        os.kill(parent_pid, 0)
        return True
    except OSError:
        return False

def port_is_free():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            return sock.connect_ex((host, port)) != 0
    except Exception:
        return True

deadline = time.time() + 30
while time.time() < deadline and (parent_alive() or not port_is_free()):
    time.sleep(0.25)

os.chdir(base_dir)
subprocess.Popen([python_exe, script_path], cwd=base_dir, close_fds=True, start_new_session=True)
"""


def register_system_admin_routes(app, deps):
    ANCHOR_DIR = deps["ANCHOR_DIR"]
    module_base_dir = os.path.realpath(os.path.join(os.path.dirname(__file__), ".."))
    raw_base_dir = deps["BASE_DIR"]
    BASE_DIR = (
        os.path.realpath(raw_base_dir)
        if os.path.isabs(raw_base_dir)
        else os.path.realpath(os.path.join(module_base_dir, raw_base_dir))
    )
    REPORTS_DIR = deps.get("REPORTS_DIR") or os.environ.get("HTML_LEARNING_REPORTS_DIR") or os.path.join(
        os.environ.get("HACKME_RUNTIME_DIR") or default_runtime_root(),
        "reports",
    )
    GIT_REPO_DIR = deps.get("GIT_REPO_DIR") or BASE_DIR
    CHAT_DIR = deps["CHAT_DIR"]
    DB_DIR = deps.get("DB_DIR") or os.path.dirname(deps["DB_PATH"])
    DB_PATH = deps["DB_PATH"]
    ADDITIONAL_DB_PATHS = {
        "auth": deps.get("AUTH_DB_PATH"),
        "audit": deps.get("AUDIT_DB_PATH"),
        "control": deps.get("CONTROL_DB_PATH"),
        "storage_catalog": deps.get("STORAGE_CATALOG_DB_PATH"),
        "points_chain": deps.get("POINTS_CHAIN_DB_PATH"),
        "trading": deps.get("TRADING_DB_PATH"),
        "finance": deps.get("FINANCE_DB_PATH"),
        "jobs": deps.get("JOBS_DB_PATH"),
        "chess_engine": deps.get("CHESS_ENGINE_DB_PATH"),
    }
    LOG_DIR = deps["LOG_DIR"]
    SERVER_LOG_PATH = deps["SERVER_LOG_PATH"]
    STORAGE_DIR = deps.get("STORAGE_DIR")
    CURRENT_SERVER_BIND_STATE = deps.get("CURRENT_SERVER_BIND_STATE") or {}
    CERT_FILE = deps.get("CERT_FILE") or os.path.join(default_runtime_root(), "cert.pem")
    KEY_FILE = deps.get("KEY_FILE") or os.path.join(default_runtime_root(), "key.pem")
    activate_emergency_lockdown = deps["activate_emergency_lockdown"]
    audit = deps["audit"]
    get_client_ip = deps["get_client_ip"]
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_audit_db = deps.get("get_audit_db", deps["get_db"])
    get_auth_db = deps.get("get_auth_db", deps["get_db"])
    get_db = deps["get_db"]
    get_ua = deps.get("get_ua", lambda: "-")
    get_feature_settings = deps["get_feature_settings"]
    get_server_output = deps.get("get_server_output", lambda limit=200: {"lines": [], "max_lines": 0})
    get_system_settings = deps["get_system_settings"]
    is_audit_chain_enabled = deps["is_audit_chain_enabled"]
    json_resp = deps["json_resp"]
    repair_audit_chain = deps["repair_audit_chain"]
    repair_violation_chains = deps["repair_violation_chains"]
    require_csrf = deps["require_csrf"]
    require_csrf_safe = deps["require_csrf_safe"]
    role_rank = deps["role_rank"]
    points_service = deps.get("points_service")
    save_feature_settings = deps["save_feature_settings"]
    save_settings = deps["save_settings"]
    server_mode_service = deps.get("server_mode_service")
    snapshot_service = deps.get("snapshot_service")
    integrity_guard = deps.get("integrity_guard")
    verify_audit_integrity = deps["verify_audit_integrity"]

    def _notify_root(type, title, body, *, link="/security", once=False):
        conn = get_db()
        try:
            created = create_root_notification_if_enabled(
                conn,
                type=type,
                title=title,
                body=body,
                link=link,
                once=once,
            )
            conn.commit()
            return created
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return 0
        finally:
            conn.close()

    def _security_signal_label(name):
        return {
            "audit_chain_broken": "審計鏈異常",
            "maintenance_mode": "維護模式已開啟",
            "pending_chat_reports": "待處理聊天檢舉過多",
            "pending_appeals": "待處理申覆過多",
            "pending_moderation_proposals": "待處理治理提案過多",
            "quarantined_files": "隔離檔案過多",
            "unknown_encrypted_files": "未知加密檔案過多",
            "count_errors": "健康度統計讀取異常",
        }.get(str(name or ""), str(name or "安全事件"))

    def _format_security_signal_notification(item):
        label = _security_signal_label(item.get("name"))
        level = str(item.get("level") or "warning").lower()
        level_label = "嚴重" if level == "critical" else "警告"
        detail = str(item.get("detail") or "").strip()
        value = item.get("value")
        threshold = item.get("threshold")
        lines = [f"{level_label}：{label}。"]
        if value not in (None, ""):
            lines.append(f"目前狀態：{value}")
        if threshold not in (None, ""):
            lines.append(f"判定門檻：{threshold}")
        if detail:
            lines.append(f"補充說明：{detail}")
        if item.get("name") == "count_errors":
            lines.append("建議處理：請到安全中心健康度頁面重新整理，若持續出現，代表部分資料表或統計查詢需要修復。")
        elif item.get("name") == "audit_chain_broken":
            lines.append("建議處理：請先停止高風險操作，進入安全中心查看審計鏈報告並執行鏈修復。")
        else:
            lines.append("建議處理：請到安全中心查看詳細資料並依事件類型處理。")
        return f"安全警訊：{label}", "\n".join(lines)

    def default_schedule_server_restart(*, reason, delay_seconds=1.25):
        if app.testing:
            return {"mode": "testing", "pid": os.getpid(), "reason": reason}

        script_path = os.path.join(BASE_DIR, "server.py")
        python_exe = sys.executable or "python3"
        host = CURRENT_SERVER_BIND_STATE.get("host") or os.environ.get("HTML_LEARNING_HOST") or "127.0.0.1"
        probe_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
        port = int(CURRENT_SERVER_BIND_STATE.get("port") or os.environ.get("HTML_LEARNING_PORT") or 5000)

        def restart_delayed():
            time.sleep(delay_seconds)
            env = os.environ.copy()
            env["HTML_LEARNING_HOST"] = host
            env["HTML_LEARNING_PORT"] = str(port)
            subprocess.Popen(
                [python_exe, "-c", restart_launcher_code(), python_exe, script_path, BASE_DIR, str(os.getpid()), probe_host, str(port)],
                cwd=BASE_DIR,
                close_fds=True,
                start_new_session=True,
                env=env,
            )
            os._exit(0)

        threading.Thread(target=restart_delayed, name="server-restart", daemon=True).start()
        return {
            "mode": "detached-restart",
            "pid": os.getpid(),
            "delay_seconds": delay_seconds,
            "host": host,
            "probe_host": probe_host,
            "port": port,
            "reason": reason,
        }

    schedule_server_restart = deps.get("schedule_server_restart") or default_schedule_server_restart

    def require_root_actor():
        actor = get_current_user_ctx()
        if not actor:
            return None, (json_resp({"ok":False,"msg":"未登入"}), 401)
        if actor["username"] != "root":
            return None, (json_resp({"ok":False,"msg":"只有 root 可執行此操作"}), 403)
        return actor, None

    def require_super_admin_actor():
        actor = get_current_user_ctx()
        if not actor:
            return None, (json_resp({"ok":False,"msg":"未登入"}), 401)
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if role_rank(actor_role) < role_rank("super_admin"):
            return None, (json_resp({"ok":False,"msg":"只有最高管理者可查看健康中心"}), 403)
        return actor, None

    def _is_sensitive_setting_key(key):
        lowered = str(key or "").lower()
        return any(marker in lowered for marker in ("password", "secret", "token", "hash", "key"))

    def _audit_setting_value(key, value):
        if _is_sensitive_setting_key(key):
            return "<redacted>" if str(value or "") else ""
        return value

    def _audit_settings_changed(event_type, actor, before, saved, *, scope="", extra=None):
        before = dict(before or {})
        saved = dict(saved or {})
        changes = []
        for key in sorted(saved):
            old_value = before.get(key)
            new_value = saved.get(key)
            changes.append({
                "key": key,
                "old": _audit_setting_value(key, old_value),
                "new": _audit_setting_value(key, new_value),
                "changed": old_value != new_value,
            })
        detail = {
            "scope": scope or event_type,
            "changed_keys": [row["key"] for row in changes if row["changed"]],
            "keys": [row["key"] for row in changes],
            "changes": changes,
        }
        if extra:
            detail["extra"] = extra
        audit(
            event_type,
            get_client_ip(),
            user=actor["username"] if actor else "-",
            success=True,
            ua=get_ua(),
            detail=json.dumps(detail, ensure_ascii=False, sort_keys=True),
        )

    def _force_points_block(reason, actor):
        if not points_service:
            return None
        result = points_service.force_seal_block(actor=actor, reason=reason)
        if result.get("sealed"):
            block = result.get("block") or {}
            audit(
                "POINTS_FORCE_BLOCK_SEALED",
                get_client_ip(),
                user=actor["username"] if actor else "-",
                success=True,
                ua=get_ua(),
                detail=f"reason={reason},block_number={block.get('block_number')},ledger_count={block.get('ledger_count')}",
            )
        elif result.get("ok") is False:
            audit(
                "POINTS_FORCE_BLOCK_FAILED",
                get_client_ip(),
                user=actor["username"] if actor else "-",
                success=False,
                ua=get_ua(),
                detail=f"reason={reason},msg={result.get('msg')}",
            )
        return result

    def run_git_command(args, *, timeout=30):
        command = ["git", "-C", GIT_REPO_DIR, *args]
        completed = subprocess.run(
            command,
            cwd=GIT_REPO_DIR,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": (completed.stdout or "").strip(),
            "stderr": (completed.stderr or "").strip(),
            "command": ["git", "-C", ".", *args],
        }

    def git_repo_ready():
        result = run_git_command(["rev-parse", "--show-toplevel"], timeout=10)
        if result["ok"]:
            return {"ok": True, "repo_dir": GIT_REPO_DIR}
        msg = "目前執行中的副本不含 Git 更新資訊"
        detail = git_short_text(result) or "not a git repository"
        if GIT_REPO_DIR != BASE_DIR:
            msg = "GitHub 版本檢查指定的 repo 無法讀取"
        return {
            "ok": False,
            "msg": msg,
            "error": detail,
            "repo_dir": GIT_REPO_DIR,
        }

    def git_short_text(result, limit=12000):
        text = "\n".join(part for part in (result.get("stdout"), result.get("stderr")) if part)
        return text[:limit]

    def read_update_summary(limit=12000):
        path = os.path.join(GIT_REPO_DIR, "docs", "UPDATE_SUMMARY.md")
        if not os.path.exists(path):
            path = os.path.join(BASE_DIR, "docs", "UPDATE_SUMMARY.md")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()[:limit]
        except OSError:
            return ""

    def read_update_summary_from_ref(ref, limit=12000):
        ref = str(ref or "").strip()
        if not ref:
            return ""
        result = run_git_command(["show", f"{ref}:docs/UPDATE_SUMMARY.md"], timeout=30)
        if result["ok"]:
            return (result["stdout"] or "")[:limit]
        return ""

    def current_git_state(fetch=False):
        ready = git_repo_ready()
        if not ready.get("ok"):
            return ready
        if fetch:
            fetch_result = run_git_command(["fetch", "--prune", "origin"], timeout=90)
            if not fetch_result["ok"]:
                return {"ok": False, "msg": "GitHub 分支資料更新失敗", "error": git_short_text(fetch_result)}
        branch_result = run_git_command(["rev-parse", "--abbrev-ref", "HEAD"])
        commit_result = run_git_command(["rev-parse", "--short", "HEAD"])
        status_result = run_git_command(["status", "--porcelain"])
        remote_result = run_git_command(["remote", "get-url", "origin"])
        refs_result = run_git_command(["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin"])
        if not branch_result["ok"] or not commit_result["ok"] or not status_result["ok"]:
            return {"ok": False, "msg": "目前 Git 狀態讀取失敗", "error": git_short_text(branch_result) or git_short_text(commit_result) or git_short_text(status_result)}
        branches = []
        if refs_result["ok"]:
            for ref in refs_result["stdout"].splitlines():
                ref = ref.strip()
                if not ref or ref == "origin/HEAD" or not ref.startswith("origin/"):
                    continue
                branch = ref.removeprefix("origin/")
                if validate_git_branch_name(branch):
                    branches.append(branch)
        return {
            "ok": True,
            "current_branch": branch_result["stdout"],
            "current_commit": commit_result["stdout"],
            "origin_url": remote_result["stdout"] if remote_result["ok"] else "",
            "repo_dir": public_relative_path(GIT_REPO_DIR, BASE_DIR),
            "dirty": bool(status_result["stdout"]),
            "dirty_files": status_result["stdout"].splitlines()[:80],
            "branches": sorted(set(branches)),
            "release_summary": read_update_summary(),
        }

    def git_update_preview(branch, *, fetch=True):
        branch = validate_git_branch_name(branch)
        if not branch:
            return {"ok": False, "msg": "分支名稱格式不合法"}
        state = current_git_state(fetch=fetch)
        if not state.get("ok"):
            return state
        remote_ref = f"origin/{branch}"
        verify = run_git_command(["rev-parse", "--verify", remote_ref])
        if not verify["ok"]:
            return {"ok": False, "msg": f"找不到遠端分支 {remote_ref}", "state": state}
        ahead_behind = run_git_command(["rev-list", "--left-right", "--count", f"HEAD...{remote_ref}"])
        name_status = run_git_command(["diff", "--name-status", "HEAD", remote_ref, "--"], timeout=60)
        stat = run_git_command(["diff", "--stat", "HEAD", remote_ref, "--"], timeout=60)
        summary = {"ahead": None, "behind": None}
        if ahead_behind["ok"]:
            parts = ahead_behind["stdout"].split()
            if len(parts) >= 2:
                summary = {"ahead": int(parts[0]), "behind": int(parts[1])}
        changed_files = []
        if name_status["ok"]:
            for line in name_status["stdout"].splitlines()[:300]:
                parts = line.split("\t")
                changed_files.append({"status": parts[0], "path": parts[-1] if parts else line})
        return {
            "ok": True,
            "branch": branch,
            "remote_ref": remote_ref,
            "state": state,
            "summary": summary,
            "changed_files": changed_files,
            "diff_stat": stat["stdout"] if stat["ok"] else git_short_text(stat),
            "release_summary": read_update_summary_from_ref(remote_ref),
            "warning": SERVER_UPDATE_WARNING,
            "strategy": "git fetch + git diff preview only",
        }

    def rebuild_integrity_baseline_after_update(actor, branch, preview):
        if not integrity_guard:
            return {"ok": False, "msg": "Integrity Guard 服務目前無法使用"}
        try:
            changed_paths = []
            for item in preview.get("changed_files") or []:
                path = str((item or {}).get("path") or "").strip()
                if path:
                    changed_paths.append(path)
            note = f"server update baseline refresh from origin/{branch}"
            baseline = integrity_guard.rebaseline_paths(
                actor=actor["username"],
                file_paths=changed_paths,
                note=note,
            )
            scan = integrity_guard.scan(actor=actor["username"], create_initial_manifest=False)
            return {"ok": bool(scan.get("ok", True)), "baseline": baseline, "result": scan}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def run_integrity_scan_after_update(actor):
        if not integrity_guard:
            return {"ok": False, "msg": "Integrity Guard 服務目前無法使用"}
        try:
            result = integrity_guard.scan(actor=actor["username"], create_initial_manifest=False)
            return {"ok": bool(result.get("ok", True)), "result": result}
        except Exception as exc:
            return {"ok": False, "msg": str(exc)}

    def prepare_server_update_recovery_points(actor, branch):
        if not snapshot_service:
            return {"ok": False, "msg": "Snapshot 服務目前無法使用"}
        if not points_service:
            return {"ok": False, "msg": "PointsChain 服務目前無法使用"}
        snapshot = snapshot_service.create_snapshot(
            snapshot_type="pre_update",
            actor=actor,
            notes=f"Before GitHub server update from origin/{branch}",
        )
        if not snapshot.ok:
            return {
                "ok": False,
                "msg": "更新前 snapshot 建立失敗，已中止更新",
                "snapshot": {"ok": False, "snapshot_id": snapshot.snapshot_id, "status": snapshot.status, "error": snapshot.error},
            }
        verification = points_service.verify_chain_bounded_snapshot()
        if verification.get("ok") is not True:
            return {
                "ok": False,
                "msg": "更新前 PointsChain bounded 驗證失敗，已中止更新",
                "snapshot": {"ok": True, "snapshot_id": snapshot.snapshot_id, "status": snapshot.status},
                "points_verification": verification,
            }
        return {
            "ok": True,
            "snapshot": {"ok": True, "snapshot_id": snapshot.snapshot_id, "status": snapshot.status},
            "points_verification": verification,
            "points_backup_policy": "disabled_append_only_chain",
        }

    def cloud_drive_storage_payload(settings):
        configured = str(settings.get("cloud_drive_storage_root") or "").strip()
        current = os.path.abspath(STORAGE_DIR) if STORAGE_DIR else ""
        effective = configured or current
        disk = storage_disk_usage(effective or current or ".")
        capacity = resolve_global_capacity_limit(disk, settings=settings)
        restart_required = False
        if configured and current:
            try:
                restart_required = os.path.realpath(configured) != os.path.realpath(current)
            except Exception:
                restart_required = configured != current
        return {
            "configured_root": configured,
            "current_root": current,
            "effective_next_root": effective,
            "restart_required": restart_required,
            "disk": disk,
            "global_capacity": capacity,
        }

    def ssl_cert_exists():
        return os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE)

    def server_ssl_payload(settings):
        return server_ssl_settings_payload(
            settings,
            current_ssl_enabled=CURRENT_SERVER_BIND_STATE.get("ssl_enabled"),
            cert_exists=ssl_cert_exists(),
        )

    def _security_test_report_root():
        path = os.path.join(REPORTS_DIR, "security", "root-triggered")
        os.makedirs(path, exist_ok=True)
        return path

    def _safe_security_test_int(value, default, minimum, maximum):
        try:
            number = int(value if value not in (None, "") else default)
        except Exception:
            return None
        if minimum <= number <= maximum:
            return number
        return None

    def tail_text_lines(path, max_lines=200):
        try:
            max_lines = max(1, min(int(max_lines), 1000))
        except Exception:
            max_lines = 200
        if not path or not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\n") for line in deque(handle, maxlen=max_lines)]

    def _security_test_job_payload(job):
        if not job:
            return None
        log_path = job.get("log_path") or ""
        log_abs = os.path.join(BASE_DIR, log_path) if log_path and not os.path.isabs(log_path) else log_path
        log_tail = []
        if log_abs:
            try:
                log_tail = tail_text_lines(log_abs, 80)
            except Exception:
                log_tail = []
        progress = job.get("progress_percent")
        if progress is None:
            progress = 100 if job.get("status") in {"passed", "failed"} else 10
        payload = {key: job.get(key) for key in (
            "job_id",
            "kind",
            "status",
            "started_at",
            "finished_at",
            "returncode",
            "command_label",
            "report_root",
            "report_dir",
            "report_artifacts",
            "log_path",
            "error",
        )}
        payload["progress_percent"] = progress
        payload["log_tail"] = log_tail
        if "production_report" in job:
            payload["production_report"] = job["production_report"]
        return payload

    def _report_prefixes(prefix):
        if isinstance(prefix, (list, tuple, set)):
            return tuple(str(item) for item in prefix if str(item))
        if prefix:
            return (str(prefix),)
        return ()

    def _find_latest_report_artifacts(report_root, prefix, started_at_ts):
        prefixes = _report_prefixes(prefix)
        try:
            candidates = []
            for name in os.listdir(report_root):
                full = os.path.join(report_root, name)
                if not (os.path.isdir(full) or os.path.isfile(full)):
                    continue
                if prefixes and not any(name.startswith(item) for item in prefixes):
                    continue
                try:
                    mtime = os.path.getmtime(full)
                except Exception:
                    mtime = 0
                if mtime + 2 >= started_at_ts:
                    candidates.append((mtime, full))
            candidates.sort(reverse=True)
            return [os.path.relpath(path, BASE_DIR) for _, path in candidates[:5]]
        except Exception:
            return []

    def _find_latest_report_dir(report_root, prefix, started_at_ts):
        for artifact in _find_latest_report_artifacts(report_root, prefix, started_at_ts):
            if os.path.isdir(os.path.join(BASE_DIR, artifact)):
                return artifact
        return ""

    def _production_report_type_for_security_job(kind):
        return {
            "privilege": "permission",
        }.get(str(kind or "").strip(), str(kind or "").strip())

    def _maybe_upload_production_report(*, kind, report_artifacts, started_ts, actor_username, client_ip, job_id):
        """Auto-upload a passed security-test report to ServerModeService so
        the production gate can see it. Returns a dict suitable to attach to
        ``job["production_report"]``:

          - ``{"ok": True, "report_type": ..., "report_id": ...}`` on success
          - ``{"ok": False, "skipped": True, "reason": "..."}`` when we choose
            not to upload (e.g. stress run had failures)
          - ``{"ok": False, "skipped": False, "reason": "..."}`` on errors
        """
        report_type = _production_report_type_for_security_job(kind)
        if not server_mode_service:
            return {"ok": False, "skipped": True, "reason": "server_mode_service_unavailable", "report_type": report_type}
        json_path = ""
        for artifact in report_artifacts or []:
            full = os.path.join(BASE_DIR, artifact)
            if os.path.isfile(full) and full.endswith(".json"):
                json_path = full
                break
        raw_report = {
            "report_type": report_type,
            "security_test_kind": kind,
            "status": "pass",
            "summary": f"job {job_id} passed",
        }
        if json_path:
            try:
                with open(json_path, "r", encoding="utf-8") as fh:
                    raw_report = json.load(fh)
            except Exception:
                pass
        if kind == "stress":
            failed = int(raw_report.get("failed_count") or 0)
            server_errors = int(raw_report.get("server_error_count") or 0)
            if failed > 0 or server_errors > 0:
                return {
                    "ok": False,
                    "skipped": True,
                    "reason": "report_not_clean",
                    "report_type": report_type,
                }
        try:
            current_target = {}
            if hasattr(server_mode_service, "_current_production_target"):
                try:
                    current_target = server_mode_service._current_production_target() or {}
                except Exception:
                    current_target = {}
            attestation = server_mode_service._prepare_production_report_attestation(
                report_type=report_type,
                raw_report=raw_report,
                target_commit=str(current_target.get("target_commit") or ""),
                target_branch=str(current_target.get("target_branch") or ""),
                server_mode=str(current_target.get("server_mode") or ""),
                test_result="pass",
                tester=actor_username or "root",
            )
        except Exception as exc:
            return {"ok": False, "skipped": False, "reason": f"attestation_error:{exc}", "report_type": report_type}
        if not attestation or not attestation.get("ok"):
            return {"ok": False, "skipped": False, "reason": "attestation_failed", "report_type": report_type}
        try:
            upload = server_mode_service.upload_production_report(
                report_type=report_type,
                test_result="pass",
                report_hash=attestation.get("report_hash"),
                signature=attestation.get("signature"),
                key_version=attestation.get("key_version"),
                raw_report=raw_report,
                tester=actor_username or "root",
                target_commit=str(current_target.get("target_commit") or ""),
                target_branch=str(current_target.get("target_branch") or ""),
                server_mode=str(current_target.get("server_mode") or ""),
            )
        except Exception as exc:
            return {"ok": False, "skipped": False, "reason": f"upload_error:{exc}", "report_type": report_type}
        if not upload or not upload.get("ok"):
            return {"ok": False, "skipped": False, "reason": "upload_failed", "report_type": report_type}
        return {
            "ok": True,
            "skipped": False,
            "report_type": report_type,
            "security_test_kind": kind,
            "report_id": upload.get("report_id"),
            "report_hash": attestation.get("report_hash"),
        }

    def _start_security_test_job(kind, command, *, command_label, report_root, report_prefix, actor, env=None):
        job_id = f"{kind}_{uuid.uuid4().hex[:12]}"
        started_ts = time.time()
        # actor may be a sqlite3.Row (no .get) or a dict (has .get) depending
        # on caller. Both support __getitem__ but Row raises if the key is
        # absent, so guard with a small helper.
        def _username(a):
            if a is None:
                return "root"
            try:
                return a["username"]
            except (KeyError, IndexError):
                return "root"
        actor_username = _username(actor)
        client_ip = get_client_ip()
        log_dir = os.path.join(report_root, "_jobs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"{job_id}.log")
        job = {
            "job_id": job_id,
            "kind": kind,
            "status": "running",
            "started_at": datetime.now().isoformat(),
            "finished_at": "",
            "returncode": None,
            "command_label": command_label,
            "report_root": os.path.relpath(report_root, BASE_DIR),
            "report_dir": "",
            "report_artifacts": [],
            "log_path": os.path.relpath(log_path, BASE_DIR),
            "error": "",
            "actor": actor_username,
            "progress_percent": 10,
        }
        with SECURITY_TEST_JOBS_LOCK:
            SECURITY_TEST_JOBS[job_id] = job

        def runner():
            proc_env = os.environ.copy()
            if env:
                proc_env.update(env)
            try:
                with open(log_path, "w", encoding="utf-8") as log_file:
                    log_file.write(f"$ {' '.join(command_label)}\n\n")
                    log_file.flush()
                    proc = subprocess.Popen(
                        command,
                        cwd=BASE_DIR,
                        env=proc_env,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    code = proc.wait()
                status = "passed" if code == 0 else "failed"
                report_dir = _find_latest_report_dir(report_root, report_prefix, started_ts)
                report_artifacts = _find_latest_report_artifacts(report_root, report_prefix, started_ts)
                production_report = None
                if status == "passed":
                    production_report = _maybe_upload_production_report(
                        kind=kind,
                        report_artifacts=report_artifacts,
                        started_ts=started_ts,
                        actor_username=actor_username,
                        client_ip=client_ip,
                        job_id=job_id,
                    )
                with SECURITY_TEST_JOBS_LOCK:
                    job.update({
                        "status": status,
                        "finished_at": datetime.now().isoformat(),
                        "returncode": code,
                        "report_dir": report_dir,
                        "report_artifacts": report_artifacts,
                        "progress_percent": 100,
                    })
                    if production_report is not None:
                        job["production_report"] = production_report
                audit("SECURITY_TEST_FINISHED", client_ip, user=actor_username, success=(code == 0), detail=f"job_id={job_id},kind={kind},returncode={code},report_dir={report_dir}")
            except Exception as exc:
                with SECURITY_TEST_JOBS_LOCK:
                    job.update({
                        "status": "failed",
                        "finished_at": datetime.now().isoformat(),
                        "returncode": None,
                        "error": str(exc),
                        "progress_percent": 100,
                    })
                audit("SECURITY_TEST_FAILED", client_ip, user=actor_username, success=False, detail=f"job_id={job_id},kind={kind},error={exc}")

        threading.Thread(target=runner, name=f"security-test-{job_id}", daemon=True).start()
        audit("SECURITY_TEST_STARTED", client_ip, user=actor_username, success=True, detail=f"job_id={job_id},kind={kind},command={command_label}")
        return job

    def table_exists(conn, table):
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        ).fetchone()
        return row is not None

    def safe_count(conn, table, where="", params=(), optional=False):
        if optional and not table_exists(conn, table):
            return 0, None
        try:
            sql = f"SELECT COUNT(*) AS c FROM {table}"
            if where:
                sql += f" WHERE {where}"
            row = conn.execute(sql, params).fetchone()
            return int(row["c"] or 0), None
        except Exception as exc:
            return 0, str(exc)

    def dir_stats(path, suffix=None, recursive=False):
        if not path or not os.path.isdir(path):
            return {"exists": False, "files": 0, "bytes": 0, "path": path}
        files = []
        if recursive:
            for root, _dirs, names in os.walk(path):
                for name in names:
                    full = os.path.join(root, name)
                    if not os.path.isfile(full):
                        continue
                    if suffix and not name.endswith(suffix):
                        continue
                    files.append(full)
        else:
            for name in os.listdir(path):
                full = os.path.join(path, name)
                if not os.path.isfile(full):
                    continue
                if suffix and not name.endswith(suffix):
                    continue
                files.append(full)
        return {
            "exists": True,
            "files": len(files),
            "bytes": sum(os.path.getsize(item) for item in files),
            "path": path,
        }

    def audit_integrity_summary():
        audit_enabled = is_audit_chain_enabled()
        if not audit_enabled:
            return {
                "enabled": False,
                "ok": None,
                "broken_at": None,
                "details": "audit chain disabled",
                "operator_action_required": False,
                "auto_lockdown_applied": False,
            }
        audit_ok, audit_broken, audit_details = verify_audit_integrity()
        return {
            "enabled": True,
            "ok": audit_ok,
            "broken_at": audit_broken,
            "details": audit_details,
            "operator_action_required": audit_ok is False,
            "auto_lockdown_applied": False,
        }

    def db_integrity_summary():
        conn = get_db()
        try:
            quick_rows = conn.execute("PRAGMA quick_check").fetchall()
            quick_check = [row[0] for row in quick_rows]
            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            foreign_key_violations = [dict(row) for row in fk_rows]
            schema_version = get_schema_version(conn)
            return {
                "ok": quick_check == ["ok"] and not foreign_key_violations and schema_version == CURRENT_SCHEMA_VERSION,
                "quick_check": quick_check,
                "foreign_key_violations": foreign_key_violations,
                "schema_version": schema_version,
                "expected_schema_version": CURRENT_SCHEMA_VERSION,
            }
        finally:
            conn.close()

    def db_schema_summary():
        conn = get_db()
        try:
            schema_version = get_schema_version(conn)
            return {
                "ok": schema_version == CURRENT_SCHEMA_VERSION,
                "quick_check": ["skipped_fast_health"],
                "foreign_key_violations": [],
                "schema_version": schema_version,
                "expected_schema_version": CURRENT_SCHEMA_VERSION,
                "details": "quick_check and foreign_key_check skipped; use /api/admin/health/db-integrity",
            }
        finally:
            conn.close()

    def health_counts():
        conn = get_db()
        auth_conn = get_auth_db()
        errors = {}
        try:
            now = datetime.now().isoformat()
            counts = {}
            for key, table, where, params, optional in (
                ("users_total", "users", "", (), False),
                ("active_users", "users", "status='active'", (), False),
                ("chat_messages", "chat_messages", "", (), True),
                ("pending_chat_reports", "chat_message_reports", "status='pending'", (), True),
                ("pending_appeals", "violation_appeals", "status='pending'", (), True),
                ("pending_moderation_proposals", "moderation_proposals", "status='pending'", (), True),
                ("pending_board_reviews", "forum_boards", "status='pending'", (), True),
                ("pending_thread_reviews", "forum_threads", "status='pending'", (), True),
                ("violations_total", "secure_violations", "", (), True),
                ("audit_entries", "secure_audit", "", (), True),
                ("uploaded_files", "uploaded_files", "deleted_at IS NULL", (), True),
                ("quarantined_files", "uploaded_files", "scan_status='quarantined' OR risk_level='blocked'", (), True),
                ("unknown_encrypted_files", "uploaded_files", "risk_level='unknown_encrypted'", (), True),
            ):
                value, err = safe_count(conn, table, where, params, optional=optional)
                counts[key] = value
                if err:
                    errors[key] = err
            active_sessions, err = safe_count(
                auth_conn,
                "sessions",
                "expires_at>? AND COALESCE(is_revoked, 0)=0",
                (now,),
                optional=True,
            )
            counts["active_sessions"] = active_sessions
            if err:
                errors["active_sessions"] = err
            return counts, errors
        finally:
            auth_conn.close()
            conn.close()

    def readiness_summary(db=None, audit_state=None):
        settings = get_system_settings()
        db = db or db_integrity_summary()
        audit_state = audit_state or audit_integrity_summary()
        checks = []

        def add_check(name, ok, detail="", severity="critical"):
            checks.append({"name": name, "ok": bool(ok), "detail": detail, "severity": severity})

        add_check("database_integrity", db["ok"], f"schema={db['schema_version']}/{db['expected_schema_version']}")
        add_check("database_file", os.path.exists(DB_PATH), DB_PATH)
        add_check("chat_dir", os.path.isdir(CHAT_DIR), CHAT_DIR, severity="degraded")
        add_check("log_dir", os.path.isdir(LOG_DIR), LOG_DIR, severity="degraded")
        add_check("anchor_dir", os.path.isdir(ANCHOR_DIR), ANCHOR_DIR, severity="degraded")
        if STORAGE_DIR:
            add_check("storage_dir", os.path.isdir(STORAGE_DIR), STORAGE_DIR, severity="degraded")
        add_check("audit_chain", audit_state["ok"] is not False, audit_state["details"], severity="critical")
        add_check("maintenance_mode", not bool(settings.get("maintenance_mode", False)), "maintenance_mode=true" if settings.get("maintenance_mode", False) else "off", severity="degraded")

        if snapshot_service:
            try:
                snapshots = snapshot_service.list_snapshots(actor={"id": 0, "username": "system"})
                add_check("snapshot_service", True, f"snapshots={len(snapshots)}", severity="degraded")
            except Exception as exc:
                add_check("snapshot_service", False, str(exc), severity="degraded")
        else:
            add_check("snapshot_service", False, "unavailable", severity="degraded")
        if integrity_guard:
            try:
                integrity = integrity_guard.status()
                high_pending = int((integrity.get("summary") or {}).get("high_risk_pending") or 0)
                pending = int((integrity.get("summary") or {}).get("pending") or 0)
                integrity_health = integrity.get("health") or {}
                detail = f"pending={pending},high={high_pending}"
                if integrity_health.get("detail"):
                    detail = f"{detail}; {integrity_health['detail']}"
                add_check(
                    "integrity_guard",
                    high_pending == 0,
                    detail,
                    severity="degraded" if integrity_health.get("level") == "degraded" else "critical",
                )
            except Exception as exc:
                add_check("integrity_guard", False, str(exc), severity="critical")

        status = "ok"
        if any((not item["ok"]) and item["severity"] == "critical" for item in checks):
            status = "critical"
        elif any(not item["ok"] for item in checks):
            status = "degraded"
        return {"status": status, "checks": checks, "database": db, "audit_integrity": audit_state}

    def anomaly_summary(audit_state=None):
        counts, errors = health_counts()
        audit_state = audit_state or audit_integrity_summary()
        settings = get_system_settings()
        signals = []

        def signal(name, level, value, threshold, detail=""):
            signals.append({"name": name, "level": level, "value": value, "threshold": threshold, "detail": detail})

        if audit_state["ok"] is False:
            signal("audit_chain_broken", "critical", audit_state["broken_at"], "ok", audit_state["details"])
        if settings.get("maintenance_mode", False):
            signal("maintenance_mode", "warning", True, False, "site is in maintenance mode")
        pending_chat_threshold = int(settings.get("security_pending_chat_reports_threshold", 10) or 10)
        pending_appeals_threshold = int(settings.get("security_pending_appeals_threshold", 10) or 10)
        pending_mod_threshold = int(settings.get("security_pending_moderation_proposals_threshold", 10) or 10)
        quarantined_threshold = int(settings.get("security_quarantined_files_threshold", 0) or 0)
        unknown_encrypted_threshold = int(settings.get("security_unknown_encrypted_files_threshold", 50) or 50)
        if counts.get("pending_chat_reports", 0) >= pending_chat_threshold:
            signal("pending_chat_reports", "warning", counts["pending_chat_reports"], pending_chat_threshold)
        if counts.get("pending_appeals", 0) >= pending_appeals_threshold:
            signal("pending_appeals", "warning", counts["pending_appeals"], pending_appeals_threshold)
        if counts.get("pending_moderation_proposals", 0) >= pending_mod_threshold:
            signal("pending_moderation_proposals", "warning", counts["pending_moderation_proposals"], pending_mod_threshold)
        if counts.get("quarantined_files", 0) > quarantined_threshold:
            signal("quarantined_files", "warning", counts["quarantined_files"], quarantined_threshold)
        if counts.get("unknown_encrypted_files", 0) >= unknown_encrypted_threshold:
            signal("unknown_encrypted_files", "info", counts["unknown_encrypted_files"], unknown_encrypted_threshold)
        if errors:
            signal("count_errors", "warning", len(errors), 0, str(errors))

        level_rank = {"ok": 0, "info": 1, "warning": 2, "critical": 3}
        status = "ok"
        for item in signals:
            if level_rank[item["level"]] > level_rank[status]:
                status = item["level"]
            if item["level"] in {"warning", "critical"}:
                title, body = _format_security_signal_notification(item)
                _notify_root(
                    "root_security_alert",
                    title,
                    body,
                    link="/security",
                    once=True,
                )
        return {"status": status, "signals": signals, "counts": counts, "errors": errors, "audit_integrity": audit_state}

    SECURITY_SETTING_KEYS = (
        "maintenance_mode",
        "allow_register",
        "server_ssl_enabled",
        "audit_chain_enabled",
        "captcha_mode",
        "feature_audit_log_enabled",
        "ip_blocking_enabled",
        "login_violation_enabled",
        "rate_limit_violation_enabled",
        "root_ip_whitelist_enabled",
        "root_ip_whitelist",
        "browser_only_mode_enabled",
        "production_single_ip_account_lock_enabled",
        "production_single_account_ip_lock_enabled",
        "integrity_guard_enabled",
        "integrity_guard_strict_mode",
        "feature_economy_enabled",
        "feature_points_chain_enabled",
    )
    SECURITY_THRESHOLD_KEYS = (
        "max_login_failures",
        "block_duration_minutes",
        "security_pending_chat_reports_threshold",
        "security_pending_appeals_threshold",
        "security_pending_moderation_proposals_threshold",
        "security_quarantined_files_threshold",
        "security_unknown_encrypted_files_threshold",
        "security_log_tail_lines",
    )

    def tail_file(path, max_lines=200):
        try:
            max_lines = max(1, min(int(max_lines), 1000))
        except Exception:
            max_lines = 200
        if not path or not os.path.exists(path):
            return {"path": path or "", "exists": False, "lines": []}
        try:
            return {"path": path, "exists": True, "lines": tail_text_lines(path, max_lines)}
        except Exception as exc:
            return {"path": path, "exists": True, "error": str(exc), "lines": []}

    def recent_secure_audit(limit=50):
        limit = max(1, min(int(limit or 50), 200))
        conn = get_audit_db()
        try:
            rows = conn.execute(
                "SELECT id, ts, action, ip, user, success, ua, detail, chain_hash "
                "FROM secure_audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "timestamp": row["ts"],
                    "action": row["action"],
                    "ip": row["ip"],
                    "actor": row["user"],
                    "success": bool(row["success"]),
                    "ua": row["ua"],
                    "details": row["detail"],
                    "_chain_hash": row["chain_hash"],
                }
                for row in rows
            ]
        except Exception:
            return []
        finally:
            conn.close()

    def security_center_payload():
        settings = get_system_settings()
        log_lines = int(settings.get("security_log_tail_lines", 200) or 200)
        audit_state = audit_integrity_summary()
        return {
            "readiness": readiness_summary(db=db_schema_summary(), audit_state=audit_state),
            "anomaly": anomaly_summary(audit_state=audit_state),
            "audit_integrity": audit_state,
            "audit_entries": recent_secure_audit(50),
            "server_log": tail_file(SERVER_LOG_PATH, log_lines),
            "server_output": get_server_output(limit=log_lines),
            "settings": {key: settings.get(key) for key in SECURITY_SETTING_KEYS},
            "thresholds": {key: settings.get(key) for key in SECURITY_THRESHOLD_KEYS},
            "features": get_feature_settings(),
            "mode": server_mode_service.get_current_mode() if server_mode_service else None,
            "profiles": server_mode_service.list_profiles() if server_mode_service else [],
        }

    def security_profile_payload(data):
        settings = data.get("settings") or {}
        thresholds = data.get("thresholds") or {}
        if isinstance(settings, str):
            try:
                settings = json.loads(settings or "{}")
            except Exception:
                return None, "settings JSON 格式錯誤"
        if isinstance(thresholds, str):
            try:
                thresholds = json.loads(thresholds or "{}")
            except Exception:
                return None, "thresholds JSON 格式錯誤"
        if not isinstance(settings, dict):
            return None, "settings 必須是 JSON object"
        if not isinstance(thresholds, dict):
            return None, "thresholds 必須是 JSON object"

        unknown_settings = sorted(str(key) for key in settings if key not in SECURITY_SETTING_KEYS)
        unknown_thresholds = sorted(str(key) for key in thresholds if key not in SECURITY_THRESHOLD_KEYS)
        if unknown_settings:
            return None, f"不支援的 settings key：{', '.join(unknown_settings)}"
        if unknown_thresholds:
            return None, f"不支援的 thresholds key：{', '.join(unknown_thresholds)}"

        normalized_thresholds = {}
        for key, value in thresholds.items():
            try:
                number = int(value)
            except Exception:
                return None, f"{key} 必須是整數"
            if number < 0 or number > 100000:
                return None, f"{key} 必須介於 0-100000"
            normalized_thresholds[key] = number

        return {"settings": {key: settings[key] for key in settings}, "thresholds": normalized_thresholds}, None

    section_ctx = {
        "ANCHOR_DIR": ANCHOR_DIR,
        "BASE_DIR": BASE_DIR,
        "CHAT_DIR": CHAT_DIR,
        "CONFIRM_APPROVE": CONFIRM_APPROVE,
        "CURRENT_SCHEMA_VERSION": CURRENT_SCHEMA_VERSION,
        "CURRENT_SERVER_BIND_STATE": CURRENT_SERVER_BIND_STATE,
        "DB_DIR": DB_DIR,
        "DB_PATH": DB_PATH,
        "ADDITIONAL_DB_PATHS": ADDITIONAL_DB_PATHS,
        "GIT_REPO_DIR": GIT_REPO_DIR,
        "LOG_DIR": LOG_DIR,
        "REPORTS_DIR": REPORTS_DIR,
        "SECURITY_SETTING_KEYS": SECURITY_SETTING_KEYS,
        "SECURITY_TEST_JOBS": SECURITY_TEST_JOBS,
        "SECURITY_TEST_JOBS_LOCK": SECURITY_TEST_JOBS_LOCK,
        "SECURITY_THRESHOLD_KEYS": SECURITY_THRESHOLD_KEYS,
        "SERVER_LOG_PATH": SERVER_LOG_PATH,
        "SERVER_UPDATE_WARNING": SERVER_UPDATE_WARNING,
        "STORAGE_DIR": STORAGE_DIR,
        "access_control_settings_payload": access_control_settings_payload,
        "anomaly_summary": anomaly_summary,
        "audit": audit,
        "audit_integrity_summary": audit_integrity_summary,
        "audit_settings_changed": _audit_settings_changed,
        "audit_storage_capacity": audit_storage_capacity,
        "cloud_drive_storage_payload": cloud_drive_storage_payload,
        "current_git_state": current_git_state,
        "db_schema_summary": db_schema_summary,
        "db_integrity_summary": db_integrity_summary,
        "dir_stats": dir_stats,
        "feature_dependency_error_payload": _feature_dependency_error_payload,
        "find_feature_dependency_violations": find_feature_dependency_violations,
        "force_points_block": _force_points_block,
        "generate_internal_test_token": generate_internal_test_token,
        "generate_maintenance_bypass_token": generate_maintenance_bypass_token,
        "get_auth_db": get_auth_db,
        "get_client_ip": get_client_ip,
        "get_current_user_ctx": get_current_user_ctx,
        "get_db": get_db,
        "get_feature_settings": get_feature_settings,
        "get_server_output": get_server_output,
        "get_system_settings": get_system_settings,
        "get_ua": get_ua,
        "git_short_text": git_short_text,
        "git_update_preview": git_update_preview,
        "hash_internal_test_token": hash_internal_test_token,
        "hash_maintenance_bypass_token": hash_maintenance_bypass_token,
        "health_counts": health_counts,
        "integrity_guard": integrity_guard,
        "is_audit_chain_enabled": is_audit_chain_enabled,
        "is_hhmm": _is_hhmm,
        "json_resp": json_resp,
        "maintenance_bypass_expires_at": maintenance_bypass_expires_at,
        "normalize_ip_whitelist_or_none": _normalize_ip_whitelist_or_none,
        "notify_root": _notify_root,
        "parse_int_in_range": _parse_int_in_range,
        "parse_strict_bool": _parse_strict_bool,
        "points_service": points_service,
        "prepare_server_update_recovery_points": prepare_server_update_recovery_points,
        "public_relative_path": public_relative_path,
        "read_update_summary": read_update_summary,
        "readiness_summary": readiness_summary,
        "rebuild_integrity_baseline_after_update": rebuild_integrity_baseline_after_update,
        "repair_audit_chain": repair_audit_chain,
        "repair_violation_chains": repair_violation_chains,
        "safe_count": safe_count,
        "require_csrf": require_csrf,
        "require_csrf_safe": require_csrf_safe,
        "require_root_actor": require_root_actor,
        "require_super_admin_actor": require_super_admin_actor,
        "role_rank": role_rank,
        "run_git_command": run_git_command,
        "safe_security_test_int": _safe_security_test_int,
        "save_feature_settings": save_feature_settings,
        "save_settings": save_settings,
        "schedule_server_restart": schedule_server_restart,
        "security_center_payload": security_center_payload,
        "security_profile_payload": security_profile_payload,
        "security_test_job_payload": _security_test_job_payload,
        "security_test_report_root": _security_test_report_root,
        "tail_text_lines": tail_text_lines,
        "server_bind_settings_payload": server_bind_settings_payload,
        "server_mode_service": server_mode_service,
        "server_ssl_payload": server_ssl_payload,
        "snapshot_service": snapshot_service,
        "start_security_test_job": _start_security_test_job,
        "validate_comfyui_api_host": validate_comfyui_api_host,
        "validate_comfyui_api_url": validate_comfyui_api_url,
        "validate_comfyui_diffusers_device": validate_comfyui_diffusers_device,
        "validate_comfyui_diffusers_device_map": validate_comfyui_diffusers_device_map,
        "validate_comfyui_diffusers_dtype": validate_comfyui_diffusers_dtype,
        "validate_comfyui_local_async_offload": validate_comfyui_local_async_offload,
        "validate_comfyui_local_attention_mode": validate_comfyui_local_attention_mode,
        "validate_comfyui_local_cache_lru": validate_comfyui_local_cache_lru,
        "validate_comfyui_local_cache_mode": validate_comfyui_local_cache_mode,
        "validate_comfyui_local_cuda_malloc": validate_comfyui_local_cuda_malloc,
        "validate_comfyui_local_precision": validate_comfyui_local_precision,
        "validate_comfyui_local_reserve_vram_gb": validate_comfyui_local_reserve_vram_gb,
        "validate_comfyui_local_text_encoder_dtype": validate_comfyui_local_text_encoder_dtype,
        "validate_comfyui_local_unet_dtype": validate_comfyui_local_unet_dtype,
        "validate_comfyui_local_upcast_attention": validate_comfyui_local_upcast_attention,
        "validate_comfyui_local_vae_dtype": validate_comfyui_local_vae_dtype,
        "validate_comfyui_local_vram_mode": validate_comfyui_local_vram_mode,
        "validate_comfyui_relative_script": validate_comfyui_relative_script,
        "validate_huggingface_api_token": validate_huggingface_api_token,
        "validate_huggingface_repo_id": validate_huggingface_repo_id,
        "normalize_huggingface_repo_id": normalize_huggingface_repo_id,
        "validate_git_branch_name": validate_git_branch_name,
        "validate_listen_host": validate_listen_host,
        "validate_listen_port": validate_listen_port,
        "verify_audit_integrity": verify_audit_integrity,
    }
    register_system_admin_security_routes(app, section_ctx)
    register_system_admin_settings_routes(app, section_ctx)
    register_system_admin_runtime_routes(app, section_ctx)
