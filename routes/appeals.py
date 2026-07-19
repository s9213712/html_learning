from datetime import datetime, timedelta
import sqlite3
from flask import request

from services.governance.sanction_notices import ensure_admin_sanction_appeal_schema, restore_admin_sanction_context
from services.governance.violation_fines import (
    active_feature_restrictions,
    list_violation_fine_appeals,
    list_violation_fines,
    mark_violation_fine_paid,
    submit_violation_fine_appeal,
)


def restored_violation_count(*, current_count, penalty_points):
    """Remove only this appeal's points from the current account counter.

    The submission snapshot is evidence, not a value that may overwrite
    violations recorded after the appeal was submitted.
    """

    return max(0, int(current_count or 0) - max(0, int(penalty_points or 0)))


def register_appeal_routes(app, deps):
    VIOLATION_APPEAL_WINDOW_HOURS = deps["VIOLATION_APPEAL_WINDOW_HOURS"]
    audit = deps["audit"]
    check_user_rate_limit = deps["check_user_rate_limit"]
    get_client_ip = deps["get_client_ip"]
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_db = deps["get_db"]
    get_latest_violation = deps["get_latest_violation"]
    json_resp = deps["json_resp"]
    normalize_text = deps["normalize_text"]
    parse_iso_to_datetime = deps["parse_iso_to_datetime"]
    parse_positive_int = deps["parse_positive_int"]
    points_service = deps.get("points_service")
    require_csrf = deps["require_csrf"]
    require_csrf_safe = deps["require_csrf_safe"]
    role_rank = deps["role_rank"]

    def _serialize_appeal_row(r):
        if not r:
            return None
        return {
            "id": r["id"],
            "user_id": r["user_id"] if "user_id" in r.keys() else None,
            "username": r["username"] if "username" in r.keys() else "",
            "latest_violation_id": r["latest_violation_id"],
            "violation_count_snapshot": r["violation_count_snapshot"],
            "penalty_points": r["penalty_points"],
            "pre_status": r["pre_status"] if ("pre_status" in r.keys()) else None,
            "pre_role": r["pre_role"] if ("pre_role" in r.keys()) else None,
            "reason": r["reason"],
            "status": r["status"],
            "reviewed_by": r["reviewed_by"],
            "reviewed_at": r["reviewed_at"],
            "review_note": r["review_note"],
            "created_at": r["created_at"],
        }

    def _serialize_violation_row(r):
        if not r:
            return None
        keys = r.keys() if hasattr(r, "keys") else {}
        return {
            "id": r["id"],
            "user_id": r["user_id"],
            "username": r["username"],
            "points": r["points"],
            "reason": r["reason"],
            "triggered_by": r["triggered_by"],
            "actor_username": r["actor_username"],
            "created_at": r["created_at"],
            "is_governance_notice": bool(r["is_governance_notice"]) if "is_governance_notice" in keys else False,
        }

    def _serialize_governance_notice_row(r):
        if not r:
            return None
        action_label = r["action_label"] if "action_label" in r.keys() else ""
        reason = r["reason"] if "reason" in r.keys() else ""
        combined_reason = action_label or "會員權益變更通知"
        if reason:
            combined_reason = f"{combined_reason}；原因：{reason}"
        return {
            "id": r["violation_id"],
            "user_id": r["user_id"],
            "username": r["username"] if "username" in r.keys() else "",
            "points": 0,
            "reason": combined_reason,
            "triggered_by": "member_governance",
            "actor_username": r["actor_username"] if "actor_username" in r.keys() else "",
            "created_at": r["created_at"],
            "is_governance_notice": True,
        }

    def _fine_charge_uuid(fine_uuid, actor_id):
        clean = str(fine_uuid or "").replace(":", "_")
        return f"violation_fine:{int(actor_id)}:{clean}"

    @app.route("/api/appeals", methods=["GET"])
    @require_csrf_safe
    def violation_appeals_list():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401

        conn = get_db()
        try:
            ensure_admin_sanction_appeal_schema(conn)
            user_id = actor["id"]
            actor_username = actor["username"]
            user_row = conn.execute(
                "SELECT violation_count FROM users WHERE id=?", (user_id,)
            ).fetchone()
            latest_violation = get_latest_violation(conn, user_id)
            violation_rows = conn.execute(
                """
                SELECT id, user_id, username, points, reason, triggered_by, actor_username, created_at
                FROM secure_violations
                WHERE user_id=?
                  AND NOT (
                      points=0
                      AND (
                          reason LIKE '會員權益變更：%'
                          OR reason LIKE '會員點數權益變更：%'
                      )
                  )
                ORDER BY id DESC LIMIT 50
                """,
                (user_id,)
            ).fetchall()
            governance_rows = conn.execute(
                """
                SELECT c.violation_id, c.user_id, u.username, c.action_label, c.reason, c.actor_username, c.created_at
                FROM admin_sanction_appeal_contexts c
                LEFT JOIN users u ON u.id = c.user_id
                LEFT JOIN secure_violations sv ON sv.id = c.violation_id
                WHERE c.user_id=?
                  AND (
                      c.violation_id < 0
                      OR (
                          sv.id IS NOT NULL
                          AND COALESCE(sv.points, 0)=0
                          AND (
                              sv.reason LIKE '會員權益變更：%'
                              OR sv.reason LIKE '會員點數權益變更：%'
                          )
                      )
                  )
                ORDER BY c.created_at DESC, c.violation_id ASC
                LIMIT 50
                """,
                (user_id,)
            ).fetchall()
            rows = conn.execute(
                "SELECT id, user_id, username, latest_violation_id, violation_count_snapshot, penalty_points, pre_status, pre_role, reason, status, reviewed_by, reviewed_at, review_note, created_at "
                "FROM violation_appeals WHERE user_id=? ORDER BY id DESC LIMIT 20",
                (user_id,)
            ).fetchall()
            appeal_by_violation = {}
            violation_ids = [row["id"] for row in violation_rows] + [row["violation_id"] for row in governance_rows]
            if violation_ids:
                placeholders = ",".join("?" for _ in violation_ids)
                appeal_rows = conn.execute(
                    "SELECT id, user_id, username, latest_violation_id, violation_count_snapshot, penalty_points, pre_status, pre_role, reason, status, reviewed_by, reviewed_at, review_note, created_at "
                    f"FROM violation_appeals WHERE user_id=? AND latest_violation_id IN ({placeholders}) ORDER BY id DESC",
                    [user_id, *violation_ids]
                ).fetchall()
                for appeal in appeal_rows:
                    vid = appeal["latest_violation_id"]
                    if vid and vid not in appeal_by_violation:
                        appeal_by_violation[vid] = appeal

            now = datetime.now()
            latest_dt = parse_iso_to_datetime(latest_violation["created_at"]) if latest_violation else None
            remaining_seconds = 0
            latest_ok = False
            if latest_dt:
                elapsed = now - latest_dt
                if elapsed <= timedelta(hours=VIOLATION_APPEAL_WINDOW_HOURS):
                    remaining_seconds = int((timedelta(hours=VIOLATION_APPEAL_WINDOW_HOURS) - elapsed).total_seconds())
                    latest_ok = True

            pending_row = conn.execute(
                "SELECT 1 FROM violation_appeals WHERE user_id=? AND status='pending' LIMIT 1",
                (user_id,)
            ).fetchone()
            violations = []
            for row in violation_rows:
                created_dt = parse_iso_to_datetime(row["created_at"])
                row_remaining = 0
                within_window = False
                if created_dt:
                    elapsed = now - created_dt
                    if elapsed <= timedelta(hours=VIOLATION_APPEAL_WINDOW_HOURS):
                        row_remaining = int((timedelta(hours=VIOLATION_APPEAL_WINDOW_HOURS) - elapsed).total_seconds())
                        within_window = True
                appeal = appeal_by_violation.get(row["id"])
                item = _serialize_violation_row(row)
                item["remaining_seconds"] = row_remaining
                appeal_status = appeal["status"] if appeal else None
                item["is_resolved"] = appeal_status == "approved"
                item["can_appeal"] = bool(within_window and not appeal and actor_username != "root")
                item["appeal"] = _serialize_appeal_row(appeal) if appeal else None
                violations.append(item)
            for row in governance_rows:
                created_dt = parse_iso_to_datetime(row["created_at"])
                row_remaining = 0
                within_window = False
                if created_dt:
                    elapsed = now - created_dt
                    if elapsed <= timedelta(hours=VIOLATION_APPEAL_WINDOW_HOURS):
                        row_remaining = int((timedelta(hours=VIOLATION_APPEAL_WINDOW_HOURS) - elapsed).total_seconds())
                        within_window = True
                appeal = appeal_by_violation.get(row["violation_id"])
                item = _serialize_governance_notice_row(row)
                item["remaining_seconds"] = row_remaining
                appeal_status = appeal["status"] if appeal else None
                item["is_resolved"] = appeal_status == "approved"
                item["can_appeal"] = bool(within_window and not appeal and actor_username != "root")
                item["appeal"] = _serialize_appeal_row(appeal) if appeal else None
                violations.append(item)

            fines, _fine_total = list_violation_fines(conn, user_id=user_id, limit=50)
            fine_appeals, _fine_appeal_total = list_violation_fine_appeals(conn, user_id=user_id, limit=50)
            feature_restrictions = active_feature_restrictions(conn, user_id=user_id)
            conn.commit()

            return json_resp({
                "ok": True,
                "latest_violation": _serialize_violation_row(latest_violation),
                "can_appeal": bool(latest_violation and latest_ok and not pending_row and actor_username != "root"),
                "remaining_seconds": remaining_seconds,
                "violation_count": user_row["violation_count"] if user_row else 0,
                "appeals": [_serialize_appeal_row(r) for r in rows],
                "violations": violations,
                "violation_fines": fines,
                "violation_fine_appeals": fine_appeals,
                "feature_restrictions": feature_restrictions,
            })
        finally:
            conn.close()

    @app.route("/api/appeals", methods=["POST"])
    @require_csrf
    def submit_violation_appeal():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_username = actor["username"]
        if actor_username == "root":
            return json_resp({"ok":False,"msg":"最高管理者無需申覆"}), 403

        conn = get_db()
        try:
            user_id = actor["id"]
            blocked, info = check_user_rate_limit(user_id, "appeal_submit", max_req=5, window_sec=3600)
            if blocked:
                return json_resp({"ok":False,"msg":f"申覆提交過於頻繁（每小時最多 {info['limit']} 次）"}), 429
            try:
                data = request.get_json(force=True)
            except Exception:
                return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
            if not isinstance(data, dict):
                return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400

            raw_violation_id = data.get("violation_id")
            violation_id = None
            if raw_violation_id not in (None, ""):
                try:
                    violation_id = int(raw_violation_id)
                except Exception:
                    return json_resp({"ok":False,"msg":"violation_id 格式錯誤"}), 400
                if violation_id == 0:
                    return json_resp({"ok":False,"msg":"violation_id 格式錯誤"}), 400
            ensure_admin_sanction_appeal_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            if violation_id is None:
                latest_violation = get_latest_violation(conn, user_id)
            elif violation_id < 0:
                latest_violation = conn.execute(
                    """
                    SELECT c.violation_id AS id, c.user_id, u.username, 0 AS points,
                           c.action_label, c.reason, c.actor_username, c.created_at
                    FROM admin_sanction_appeal_contexts c
                    LEFT JOIN users u ON u.id = c.user_id
                    WHERE c.violation_id=? AND c.user_id=?
                    """,
                    (violation_id, user_id)
                ).fetchone()
            else:
                latest_violation = conn.execute(
                    "SELECT id, user_id, username, points, reason, triggered_by, actor_username, created_at "
                    "FROM secure_violations WHERE id=? AND user_id=?",
                    (violation_id, user_id)
                ).fetchone()
            if not latest_violation:
                return json_resp({"ok":False,"msg":"找不到可申覆的違規紀錄"}), 400

            latest_dt = parse_iso_to_datetime(latest_violation["created_at"])
            if not latest_dt or datetime.now() - latest_dt > timedelta(hours=VIOLATION_APPEAL_WINDOW_HOURS):
                return json_resp({"ok":False,"msg":"超過申覆時限（24 小時）"}), 409

            existing = conn.execute(
                "SELECT 1 FROM violation_appeals WHERE user_id=? AND latest_violation_id=? LIMIT 1",
                (user_id, latest_violation["id"])
            ).fetchone()
            if existing:
                return json_resp({"ok":False,"msg":"這筆違規已提交過申覆"}), 409
            reason = normalize_text(data.get("reason"))
            if not reason:
                return json_resp({"ok":False,"msg":"請填寫申覆原因"}), 400
            if len(reason) > 200:
                return json_resp({"ok":False,"msg":"申覆原因請控制在 200 字以內"}), 400

            user_row = conn.execute(
                "SELECT id, username, violation_count, status, role FROM users WHERE id=?",
                (user_id,)
            ).fetchone()
            if not user_row:
                return json_resp({"ok":False,"msg":"帳號不存在"}), 404

            penalty_points = latest_violation["points"] if "points" in latest_violation.keys() else 0
            try:
                conn.execute(
                    "INSERT INTO violation_appeals "
                    "(user_id, username, latest_violation_id, violation_count_snapshot, penalty_points, pre_status, pre_role, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        user_id,
                        user_row["username"],
                        latest_violation["id"],
                        user_row["violation_count"],
                        penalty_points,
                        user_row["status"],
                        user_row["role"],
                        reason,
                        datetime.now().isoformat()
                    )
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return json_resp({"ok":False,"msg":"這筆違規已提交過申覆"}), 409
            conn.commit()
            audit("VIOLATION_APPEAL_SUBMITTED", get_client_ip(), user=actor_username,
                  detail=f"user_id={user_id} latest_violation_id={latest_violation['id']}")
            return json_resp({"ok":True,"msg":"申覆已提交，等待超級管理員審核"})
        finally:
            conn.close()

    @app.route("/api/violation-fines/<path:fine_uuid>/appeal", methods=["POST"])
    @require_csrf
    def submit_violation_fine_appeal_route(fine_uuid):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if actor["username"] == "root":
            return json_resp({"ok":False,"msg":"最高管理者無需罰單申覆"}), 403
        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg":"請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg":"請求內容格式錯誤"}), 400
        reason = normalize_text(data.get("reason"))
        conn = get_db()
        try:
            appeal, created = submit_violation_fine_appeal(
                conn,
                fine_uuid=fine_uuid,
                user_id=actor["id"],
                username=actor["username"],
                reason=reason,
            )
            conn.commit()
            audit("VIOLATION_FINE_APPEAL_SUBMITTED", get_client_ip(), user=actor["username"], detail=f"fine_uuid={fine_uuid}")
            return json_resp({"ok":True,"msg":"罰單申覆已提交","appeal":appeal,"created":created})
        except ValueError as exc:
            conn.rollback()
            return json_resp({"ok":False,"msg":str(exc)}), 400
        finally:
            conn.close()

    @app.route("/api/violation-fines/<path:fine_uuid>/pay", methods=["POST"])
    @require_csrf
    def pay_violation_fine_route(fine_uuid):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        if actor["username"] == "root":
            return json_resp({"ok":False,"msg":"root 不需繳罰款"}), 403
        if not points_service or not hasattr(points_service, "pay_violation_fine"):
            return json_resp({"ok":False,"msg":"積分鏈付款功能未啟用"}), 503
        try:
            data = request.get_json(force=True) or {}
        except Exception:
            return json_resp({"ok":False,"msg":"請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg":"請求內容格式錯誤"}), 400
        conn = get_db()
        try:
            fines, _total = list_violation_fines(conn, user_id=actor["id"], limit=200)
            fine = next((item for item in fines if item["fine_uuid"] == fine_uuid), None)
            if not fine:
                return json_resp({"ok":False,"msg":"找不到罰單"}), 404
            if not fine.get("is_payable"):
                return json_resp({"ok":False,"msg":"罰單已結案，不能繳款"}), 409
            conn.commit()
        finally:
            conn.close()
        try:
            request_uuid = str(data.get("request_uuid") or data.get("charge_uuid") or "").strip() or _fine_charge_uuid(fine_uuid, actor["id"])
            payment = points_service.pay_violation_fine(
                user_id=actor["id"],
                fine_uuid=fine_uuid,
                amount_points=fine.get("amount_due_points") or fine["amount_points"],
                source_wallet_address=data.get("source_wallet_address") or "",
                request_uuid=request_uuid,
                signature=data.get("signature") or data.get("wallet_signature") or "",
                actor=actor,
                metadata={"fine_reason": fine.get("reason"), "policy_key": fine.get("policy_key")},
            )
            conn = get_db()
            try:
                updated = mark_violation_fine_paid(
                    conn,
                    fine_uuid=fine_uuid,
                    payment_ledger_uuid=(payment.get("ledger") or {}).get("ledger_uuid") or "",
                    payment_charge_uuid=payment.get("charge_uuid") or request_uuid,
                    payment_source_wallet_address=data.get("source_wallet_address") or "",
                )
                conn.commit()
            finally:
                conn.close()
            audit("VIOLATION_FINE_PAID", get_client_ip(), user=actor["username"], detail=f"fine_uuid={fine_uuid} amount={fine.get('amount_due_points') or fine['amount_points']}")
            return json_resp({"ok":True,"msg":"罰款已繳清，相關限制已解除","fine":updated,"payment":payment})
        except PermissionError as exc:
            return json_resp({"ok":False,"msg":str(exc) or "付款簽章失敗"}), 403
        except Exception as exc:
            return json_resp({"ok":False,"msg":str(exc) or "罰款付款失敗"}), 400

    @app.route("/api/admin/appeals", methods=["GET"])
    @require_csrf_safe
    def admin_violation_appeals():
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if role_rank(actor_role) < role_rank("manager"):
            return json_resp({"ok":False,"msg":"權限不足"}), 403

        status = normalize_text(request.args.get("status","")) or "pending"
        page = parse_positive_int(request.args.get("page", 1))
        if page is None:
            return json_resp({"ok":False,"msg":"page 參數格式錯誤"}), 400
        limit = parse_positive_int(request.args.get("limit", 20), max_value=100)
        if limit is None:
            return json_resp({"ok":False,"msg":"limit 參數格式錯誤"}), 400
        offset = (page - 1) * limit

        conn = get_db()
        try:
            ensure_admin_sanction_appeal_schema(conn)
            where = "WHERE 1=1"
            params = []
            if status == "pending":
                where = "WHERE status IN ('pending','reviewing_approve')"
            elif status in ("approved","rejected"):
                where = "WHERE status=?"
                params.append(status)
            count_query = "SELECT COUNT(*) as c FROM violation_appeals " + where
            total = conn.execute(count_query, params).fetchone()["c"]
            rows = conn.execute(
                "SELECT id, user_id, username, latest_violation_id, violation_count_snapshot, penalty_points, pre_status, pre_role, reason, status, reviewed_by, reviewed_at, review_note, created_at "
                f"FROM violation_appeals {where} ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall()

            items = []
            for r in rows:
                items.append({
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "username": r["username"],
                    "latest_violation_id": r["latest_violation_id"],
                    "violation_count_snapshot": r["violation_count_snapshot"],
                    "penalty_points": r["penalty_points"],
                    "pre_status": r["pre_status"],
                    "pre_role": r["pre_role"],
                    "reason": r["reason"],
                    "status": r["status"],
                    "reviewed_by": r["reviewed_by"],
                    "reviewed_at": r["reviewed_at"],
                    "review_note": r["review_note"],
                    "created_at": r["created_at"]
                })
            return json_resp({
                "ok": True,
                "items": items,
                "total": total,
                "page": page,
                "limit": limit,
                "status": status
            })
        finally:
            conn.close()

    @app.route("/api/admin/appeals/<int:appeal_id>/review", methods=["POST"])
    @require_csrf
    def admin_violation_appeal_review(appeal_id):
        actor = get_current_user_ctx()
        if not actor:
            return json_resp({"ok":False,"msg":"未登入"}), 401
        actor_role = "super_admin" if actor["username"] == "root" else actor["role"]
        if actor_role != "super_admin":
            return json_resp({"ok":False,"msg":"只有最高管理者可審核申覆"}), 403

        try:
            data = request.get_json(force=True)
        except Exception:
            return json_resp({"ok":False,"msg": "請求 JSON 格式錯誤"}), 400
        if not isinstance(data, dict):
            return json_resp({"ok":False,"msg": "請求內容格式錯誤"}), 400

        action = (normalize_text(data.get("action")) or "").lower()
        if action not in ("approve", "reject"):
            return json_resp({"ok":False,"msg":"action 必須是 approve 或 reject"}), 400
        note = (normalize_text(data.get("note")) or "")[:200]

        conn = get_db()
        try:
            final_status = "approved" if action == "approve" else "rejected"
            reviewed_at = datetime.now().isoformat()
            points_ledger_uuid = None
            points_rollback = None
            ensure_admin_sanction_appeal_schema(conn)
            conn.commit()

            if action == "reject":
                conn.execute("BEGIN IMMEDIATE")
                appeal = conn.execute(
                    "SELECT * FROM violation_appeals WHERE id=?", (appeal_id,)
                ).fetchone()
                if not appeal:
                    conn.rollback()
                    return json_resp({"ok":False,"msg":"找不到申覆申請"}), 404
                if appeal["status"] != "pending":
                    conn.rollback()
                    return json_resp({"ok":False,"msg":"申覆申請已處理"}), 409
                updated = conn.execute(
                    "UPDATE violation_appeals "
                    "SET status='rejected', reviewed_by=?, reviewed_at=?, review_note=? "
                    "WHERE id=? AND status='pending'",
                    (actor["username"], reviewed_at, note, appeal_id),
                )
                if updated.rowcount != 1:
                    conn.rollback()
                    return json_resp({"ok":False,"msg":"申覆申請已由另一個審核程序處理"}), 409
                conn.commit()
            else:
                # Claim before the external points compensation.  The
                # intermediate state is retryable: compensation itself is
                # idempotent and final account mutation is guarded by a CAS.
                conn.execute("BEGIN IMMEDIATE")
                appeal = conn.execute(
                    "SELECT * FROM violation_appeals WHERE id=?", (appeal_id,)
                ).fetchone()
                if not appeal:
                    conn.rollback()
                    return json_resp({"ok":False,"msg":"找不到申覆申請"}), 404
                if appeal["status"] == "pending":
                    user_row = conn.execute(
                        "SELECT id FROM users WHERE id=?", (appeal["user_id"],)
                    ).fetchone()
                    if not user_row:
                        conn.rollback()
                        return json_resp({"ok":False,"msg":"申覆帳號已不存在"}), 404
                    claimed = conn.execute(
                        "UPDATE violation_appeals "
                        "SET status='reviewing_approve', reviewed_by=?, reviewed_at=?, review_note=? "
                        "WHERE id=? AND status='pending'",
                        (actor["username"], reviewed_at, note, appeal_id),
                    )
                    if claimed.rowcount != 1:
                        conn.rollback()
                        return json_resp({"ok":False,"msg":"申覆申請已由另一個審核程序認領"}), 409
                    conn.commit()
                elif appeal["status"] == "reviewing_approve":
                    if str(appeal["reviewed_by"] or "") != actor["username"]:
                        conn.rollback()
                        return json_resp({"ok":False,"msg":"申覆申請正由另一個審核者處理"}), 409
                    conn.commit()
                else:
                    conn.rollback()
                    return json_resp({"ok":False,"msg":"申覆申請已處理"}), 409

                context = conn.execute(
                    "SELECT points_ledger_uuid FROM admin_sanction_appeal_contexts "
                    "WHERE violation_id=? AND user_id=?",
                    (appeal["latest_violation_id"], appeal["user_id"]),
                ).fetchone()
                points_ledger_uuid = context["points_ledger_uuid"] if context and context["points_ledger_uuid"] else None
                if points_ledger_uuid and not points_service:
                    return json_resp({
                        "ok": False,
                        "msg": "申覆點數補償服務不可用，申覆保留於可重試審核狀態",
                        "points_ledger_uuid": points_ledger_uuid,
                    }), 503
                if points_ledger_uuid:
                    try:
                        points_rollback = points_service.compensate_ledger(
                            actor=actor,
                            ledger_uuid=points_ledger_uuid,
                            reason=f"appeal approved #{appeal_id}: {note or 'root approved'}",
                        )
                    except Exception as exc:
                        audit("VIOLATION_APPEAL_POINTS_ROLLBACK_FAILED", get_client_ip(), user=actor["username"], success=False,
                              detail=f"appeal_id={appeal_id} ledger_uuid={points_ledger_uuid} error={exc}")
                        return json_resp({
                            "ok": False,
                            "msg": "申覆點數補償交易失敗，申覆保留於可重試審核狀態",
                            "points_ledger_uuid": points_ledger_uuid,
                        }), 500

                conn.execute("BEGIN IMMEDIATE")
                appeal = conn.execute(
                    "SELECT * FROM violation_appeals WHERE id=?", (appeal_id,)
                ).fetchone()
                if not appeal or appeal["status"] != "reviewing_approve":
                    conn.rollback()
                    return json_resp({"ok":False,"msg":"申覆申請已由另一個審核程序處理"}), 409
                user_row = conn.execute(
                    "SELECT id, username, status, role, violation_count FROM users WHERE id=?",
                    (appeal["user_id"],),
                ).fetchone()
                if not user_row:
                    conn.rollback()
                    return json_resp({"ok":False,"msg":"申覆帳號已不存在"}), 404
                restored_count = restored_violation_count(
                    current_count=user_row["violation_count"],
                    penalty_points=appeal["penalty_points"],
                )
                restored_sanction = restore_admin_sanction_context(
                    conn,
                    user_id=appeal["user_id"],
                    violation_id=appeal["latest_violation_id"],
                )
                if appeal["latest_violation_id"] < 0 and not restored_sanction:
                    conn.rollback()
                    return json_resp({"ok":False,"msg":"找不到對應的會員權益通知上下文，無法完成申覆恢復"}), 409
                conn.execute(
                    "UPDATE users SET violation_count=?, updated_at=? WHERE id=?",
                    (restored_count, reviewed_at, appeal["user_id"]),
                )
                finalized = conn.execute(
                    "UPDATE violation_appeals "
                    "SET status='approved', reviewed_by=?, reviewed_at=?, review_note=? "
                    "WHERE id=? AND status='reviewing_approve'",
                    (actor["username"], reviewed_at, note, appeal_id),
                )
                if finalized.rowcount != 1:
                    conn.rollback()
                    return json_resp({"ok":False,"msg":"申覆申請已由另一個審核程序處理"}), 409
                conn.commit()
            audit("VIOLATION_APPEAL_REVIEWED", get_client_ip(), user=actor["username"],
                  detail=f"appeal_id={appeal_id} action={action}")
            return json_resp({"ok":True,"msg": "已核准撤銷" if action == "approve" else "已維持原處分", "points_rollback": points_rollback})
        finally:
            conn.close()
