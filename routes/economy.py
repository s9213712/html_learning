import csv
import hashlib
import io
import json
import time

from flask import Response, request

from services.points_chain import (
    DISPLAY_CURRENCY,
    bind_self_custody_wallet,
    create_multisig_wallet,
    create_official_hot_wallet,
    delete_cold_wallet,
    delete_primary_cold_wallet,
    ensure_system_wallets,
    list_wallet_identities,
    system_account_wallet_onboarding_status,
    wallet_onboarding_status,
)
from services.governance.sanction_notices import record_admin_sanction_notice
from services.governance.violation_fines import assert_user_feature_allowed
from services.job_center import get_job
from services.management_plane import (
    MANAGEMENT_PLANE_SOURCE_MODULE,
    get_management_snapshot,
    management_job_start_payload,
    start_management_plane_job,
)


def register_economy_routes(app, deps):
    get_current_user_ctx = deps["get_current_user_ctx"]
    get_client_ip = deps.get("get_client_ip", lambda: "")
    get_ua = deps.get("get_ua", lambda: "")
    json_resp = deps["json_resp"]
    audit = deps.get("audit", lambda *args, **kwargs: None)
    get_db = deps.get("get_db")
    require_csrf = deps["require_csrf"]
    require_csrf_safe = deps["require_csrf_safe"]
    role_rank = deps.get("role_rank", lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0))
    points_service = deps["points_service"]
    server_mode_service = deps.get("server_mode_service")
    get_system_settings = deps.get("get_system_settings", lambda: {})

    def actor_value(actor, key, default=None):
        if not actor:
            return default
        try:
            return actor[key]
        except Exception:
            return actor.get(key, default) if hasattr(actor, "get") else default

    def actor_or_401():
        actor = get_current_user_ctx()
        if not actor:
            return None, json_resp({"ok": False, "msg": "未登入"}, 401)
        return actor, None

    def is_root_actor(actor):
        return actor_value(actor, "username") == "root"

    def manager_or_403():
        actor, err = actor_or_401()
        if err:
            return None, err
        role = "super_admin" if actor_value(actor, "username") == "root" else actor_value(actor, "role", "user")
        if role_rank(role) < role_rank("manager"):
            return None, json_resp({"ok": False, "msg": "需要管理員權限"}, 403)
        return actor, None

    def root_or_403():
        actor, err = actor_or_401()
        if err:
            return None, err
        if actor_value(actor, "username") != "root":
            return None, json_resp({"ok": False, "msg": "只有 root 可執行此操作"}, 403)
        return actor, None

    def parse_json_body():
        try:
            data = request.get_json(force=True)
        except Exception:
            return None, json_resp({"ok": False, "msg": "請求 JSON 格式錯誤"}, 400)
        if not isinstance(data, dict):
            return None, json_resp({"ok": False, "msg": "請求內容格式錯誤"}, 400)
        return data, None

    def parse_positive_int(value, *, default=1, maximum=1_000_000_000):
        try:
            number = int(value if value not in (None, "") else default)
        except Exception:
            return None
        if number < 1 or number > maximum:
            return None
        return number

    def parse_required_user_id(value):
        if value in (None, ""):
            return None
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            return None
        return user_id if user_id > 0 else None

    def active_user_wallet_rows(conn, user_id):
        return list_wallet_identities(conn, int(user_id))

    def maybe_award_initial_grants_for_actor(actor):
        if not points_service or actor_value(actor, "username") == "root":
            return None
        try:
            return points_service.award_initial_grants_after_wallet_onboarding(
                user_id=actor["id"],
                actor={"id": actor["id"], "username": actor["username"], "role": actor["role"]},
            )
        except Exception as exc:
            audit(
                "POINTS_WALLET_INITIAL_GRANT_AUTO_FAILED",
                get_client_ip(),
                user=actor_value(actor, "username"),
                success=False,
                ua=get_ua(),
                detail=str(exc),
            )
            return {"created_count": 0, "error": str(exc)}

    def maybe_award_signup_bonus_for_actor(actor):
        if not points_service or actor_value(actor, "username") == "root":
            return None
        try:
            return points_service.award_signup_bonus(
                user_id=actor["id"],
                actor={"id": actor["id"], "username": actor["username"], "role": actor["role"]},
            )
        except Exception as exc:
            audit(
                "POINTS_WALLET_SIGNUP_BONUS_AUTO_FAILED",
                get_client_ip(),
                user=actor_value(actor, "username"),
                success=False,
                ua=get_ua(),
                detail=str(exc),
            )
            return {"created": False, "error": str(exc)}

    def _stable_spend_key(*, user_id, item_key, quantity):
        payload = json.dumps(
            {
                "user_id": int(user_id),
                "item_key": str(item_key),
                "quantity": int(quantity),
                "minute_bucket": int(time.time() // 60),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "spend:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def service_error(exc):
        msg = str(exc) or exc.__class__.__name__
        status = 400
        if "insufficient balance" in msg:
            status = 409
            return json_resp({"ok": False, "msg": "點數不足，無法扣除；本次交易未寫入帳本", "code": "insufficient_balance"}), status
        if "pc0 official hot wallets are internal ledger addresses" in msg or "external or cold-chain deposits must use a platform deposit address" in msg:
            return json_resp({
                "ok": False,
                "msg": "pc0 站內託管錢包是官方託管的站內帳本地址，不是鏈上可收款地址；入金請使用平台入金地址，提領請使用提領橋流程",
                "code": "pc0_internal_address_not_chain_reachable",
                "guidance": {
                    "deposit": "外部或冷錢包先轉入平台控制的鏈上入金地址，確認後由站內帳本 credit 到 pc0 站內託管錢包。",
                    "withdrawal": "從 pc0 站內託管錢包提領時，先鎖定站內餘額，再由平台提領金庫送出鏈上交易。",
                },
            }), status
        return json_resp({"ok": False, "msg": msg}), status

    def restriction_guard(actor, feature_key):
        if not get_db or not actor or actor_value(actor, "username") == "root":
            return None
        conn = get_db()
        try:
            allowed, msg, restrictions = assert_user_feature_allowed(conn, user_id=actor_value(actor, "id"), feature_key=feature_key)
            if allowed:
                conn.commit()
                return None
            conn.commit()
            return json_resp({"ok": False, "msg": msg, "code": "feature_restricted_by_violation_fine", "restrictions": restrictions}), 423
        finally:
            conn.close()

    def setting_bool(key, *, default=False):
        try:
            value = (get_system_settings() or {}).get(key, default)
        except Exception:
            value = default
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
        return bool(value)

    def points_chain_enabled():
        return setting_bool("feature_points_chain_enabled", default=True)

    def points_chain_disabled_response():
        return json_resp({
            "ok": False,
            "code": "points_chain_disabled",
            "feature": "feature_points_chain_enabled",
            "msg": "PointsChain 私有鏈目前已關閉；基本積分錢包、帳本、服務價格與一般扣點仍可使用。",
        }, 503)

    def management_actor(actor):
        return {
            "id": actor_value(actor, "id"),
            "username": actor_value(actor, "username", ""),
            "role": actor_value(actor, "role", "user"),
        }

    def management_job_response(started, *, snapshot_key):
        payload = management_job_start_payload(
            started["job"],
            snapshot_key=snapshot_key,
            created=bool(started.get("created")),
        )
        return json_resp(payload, 202)

    def management_snapshot_response(snapshot_key, *, payload_key=None, missing_status=404):
        conn = get_db()
        try:
            sql_started = time.perf_counter()
            snapshot = get_management_snapshot(conn, snapshot_key=snapshot_key, include_payload=True)
            request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        finally:
            conn.close()
        if not snapshot.get("ok"):
            return json_resp({
                "ok": False,
                "snapshot": snapshot,
                "snapshot_key": snapshot_key,
                "client_should_enqueue": True,
            }, missing_status)
        payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
        meta = {
            "snapshot_key": snapshot.get("snapshot_key"),
            "generated_at": snapshot.get("generated_at"),
            "updated_at": snapshot.get("updated_at"),
            "source_job_uuid": snapshot.get("source_job_uuid"),
            "summary": snapshot.get("summary") or {},
            "snapshot_backed": True,
        }
        if payload_key:
            return json_resp({
                "ok": bool(payload.get("ok", True)),
                payload_key: payload.get(payload_key),
                "snapshot": meta,
            })
        response_payload = dict(payload)
        response_payload.setdefault("ok", True)
        response_payload["snapshot"] = meta
        return json_resp(response_payload)

    def management_job_status_response(job_uuid):
        conn = get_db()
        try:
            sql_started = time.perf_counter()
            job = get_job(conn, job_uuid)
            request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        finally:
            conn.close()
        if not job or str(job.get("source_module") or "") != MANAGEMENT_PLANE_SOURCE_MODULE:
            return json_resp({"ok": False, "msg": "找不到 management-plane 任務"}), 404
        return json_resp({"ok": True, "job": job})

    def csv_download_response(filename, fieldnames, rows):
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})
        return Response(
            "\ufeff" + buffer.getvalue(),
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    def load_user_ledger_for_export(user_id, *, max_rows=10000):
        rows = []
        offset = 0
        while len(rows) < max_rows:
            batch = points_service.list_ledger(user_id=user_id, limit=200, offset=offset)
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < 200:
                break
            offset += len(batch)
        return rows[:max_rows]

    def notify_member_points_action(*, actor, user_id, action_label, reason, points_ledger_uuid=None, appealable=False):
        if not get_db:
            return
        conn = get_db()
        try:
            target = conn.execute(
                "SELECT id, username, role, status, member_level, base_level, effective_level, sanction_status, sanction_until FROM users WHERE id=?",
                (int(user_id),),
            ).fetchone()
            if not target or target["username"] == "root":
                return
            previous = {key: target[key] for key in target.keys()}
            record_admin_sanction_notice(
                conn,
                actor=actor,
                target=target,
                previous=previous,
                action_label=action_label,
                reason=reason or "會員點數權益變更",
                points_ledger_uuid=points_ledger_uuid,
                appealable=appealable,
            )
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            audit(
                "MEMBER_POINTS_NOTICE_FAILED",
                get_client_ip(),
                user=actor_value(actor, "username", "admin"),
                success=False,
                ua=get_ua(),
                detail=f"user_id={user_id}, error={exc}",
            )
        finally:
            conn.close()

    @app.route("/api/points/wallet", methods=["GET"])
    @require_csrf_safe
    def points_wallet():
        actor, err = actor_or_401()
        if err:
            return err
        if is_root_actor(actor):
            return json_resp({
                "ok": True,
                "wallet": {
                    "system_account": True,
                    "active_wallet_address": "",
                    "deposit_address": "",
                    "deposit_addresses": [],
                    "points_balance": 0,
                    "points_frozen": 0,
                    "wallet_identity_balances": {},
                    "msg": "root 管理官方/系統錢包，不使用會員官方熱錢包。",
                },
                "initial_grants": None,
            })
        hydrate = str(request.args.get("hydrate") or request.args.get("full") or "").strip().lower() in {"1", "true", "yes", "on"}
        wallet = points_service.get_wallet(actor["id"]) if hydrate else points_service.get_wallet_snapshot(actor["id"])
        initial_grants = maybe_award_initial_grants_for_actor(actor)
        if initial_grants and int(initial_grants.get("created_count") or 0) > 0:
            wallet = points_service.get_wallet(actor["id"]) if hydrate else points_service.get_wallet_snapshot(actor["id"])
        return json_resp({"ok": True, "wallet": wallet, "initial_grants": initial_grants})

    @app.route("/api/points/deposit-address", methods=["GET"])
    @require_csrf_safe
    def points_deposit_address():
        actor, err = actor_or_401()
        if err:
            return err
        if is_root_actor(actor):
            return json_resp({
                "ok": True,
                "deposit_address": "",
                "deposit_addresses": [],
                "model": "system_account_no_member_deposit_address",
                "official_hot_wallet_address": "",
                "msg": "root 管理官方/系統錢包，不使用會員入金地址。",
            })
        wallet = points_service.get_wallet(actor["id"])
        return json_resp({
            "ok": True,
            "deposit_address": wallet.get("deposit_address") or "",
            "deposit_addresses": wallet.get("deposit_addresses") or [],
            "model": wallet.get("deposit_model") or "external_or_cold_chain_to_platform_deposit_address_then_internal_pc0_credit",
            "official_hot_wallet_address": wallet.get("active_wallet_address") or "",
        })

    @app.route("/api/points/wallet/onboarding", methods=["GET"])
    @require_csrf_safe
    def points_wallet_onboarding():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        conn = points_service.get_db()
        try:
            points_service.ensure_schema(conn)
            if not is_root_actor(actor):
                create_official_hot_wallet(
                    conn,
                    user_id=actor["id"],
                    chain_secret=getattr(points_service, "chain_secret", ""),
                )
                try:
                    points_service.ensure_user_deposit_address(conn, actor["id"])
                except Exception:
                    pass
            conn.commit()
        finally:
            conn.close()
        initial_grants = maybe_award_initial_grants_for_actor(actor)
        signup_bonus = maybe_award_signup_bonus_for_actor(actor)
        conn = points_service.get_db()
        try:
            points_service.ensure_schema(conn)
            status = wallet_onboarding_status(conn, points_service=points_service, user_id=actor["id"])
            if is_root_actor(actor):
                status = system_account_wallet_onboarding_status(status)
            conn.commit()
            return json_resp({"ok": True, "onboarding": status, "initial_grants": initial_grants, "signup_bonus": signup_bonus})
        finally:
            conn.close()

    @app.route("/api/points/wallet/onboarding", methods=["POST"])
    @require_csrf
    def points_wallet_onboarding_update():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        mode = str(data.get("mode") or "").strip().lower()
        if mode not in {"official_hot", "self_custody_cold", "imported_cold", "multisig"}:
            return json_resp({"ok": False, "msg": "wallet mode 不支援"}, 400)
        if is_root_actor(actor):
            return json_resp({
                "ok": False,
                "msg": "root 管理官方/系統錢包，不建立會員官方熱錢包或會員冷錢包；請使用官方錢包管理。",
                "code": "system_account_no_member_wallet",
            }, 403)
        conn = points_service.get_db()
        try:
            points_service.ensure_schema(conn)
            before_wallets = active_user_wallet_rows(conn, actor["id"])
            before_wallet_ids = {int(row["id"]) for row in before_wallets}
            before_wallet_addresses = {str(row["address"] or "").strip().lower() for row in before_wallets}
            wallet_count_before = points_service.wallet_creation_chargeable_wallet_count(conn, actor["id"])
            if mode == "official_hot":
                identity = create_official_hot_wallet(
                    conn,
                    user_id=actor["id"],
                    chain_secret=getattr(points_service, "chain_secret", ""),
                )
            elif mode in {"self_custody_cold", "imported_cold"}:
                identity = bind_self_custody_wallet(
                    conn,
                    user_id=actor["id"],
                    wallet_type=mode,
                    public_key_jwk=data.get("public_key_jwk") or {},
                    address=data.get("address") or "",
                    signature=data.get("signature") or "",
                    backup_confirmed=bool(data.get("backup_confirmed")),
                    label=data.get("label") or "",
                )
            else:
                identity = create_multisig_wallet(
                    conn,
                    user_id=actor["id"],
                    threshold=data.get("threshold"),
                    signer_addresses=data.get("signer_addresses") or [],
                    label=data.get("label") or "",
                )
            created_new_wallet = bool(mode != "official_hot" and identity and int(identity.get("id") or 0) not in before_wallet_ids)
            creation_fee = {"charged": False, "amount_points": 0}
            if created_new_wallet and wallet_count_before > 0:
                fee_source = str(data.get("fee_source_wallet_address") or "").strip().lower()
                if fee_source not in before_wallet_addresses:
                    raise ValueError("wallet creation fee must be paid from an existing active wallet")
                creation_fee = points_service.charge_wallet_creation_fee_locked(
                    conn,
                    user_id=actor["id"],
                    source_wallet_address=fee_source,
                    request_uuid=data.get("fee_request_uuid") or data.get("request_uuid") or "",
                    signature=data.get("fee_signature") or data.get("wallet_signature") or "",
                    wallet_count_before=wallet_count_before,
                    amount_points=data.get("fee_quote_amount") if data.get("fee_quote_amount") not in (None, "") else None,
                    mode=mode,
                    actor={"id": actor["id"], "username": actor["username"], "role": actor["role"]},
                )
            points_service._rebuild_wallets_from_ledger(conn)
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
            return service_error(exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        bonus = None
        initial_grants = None
        if actor_value(actor, "username") != "root":
            try:
                initial_grants = points_service.award_initial_grants_after_wallet_onboarding(
                    user_id=actor["id"],
                    actor={"id": actor["id"], "username": actor["username"], "role": actor["role"]},
                )
            except Exception as exc:
                audit("POINTS_WALLET_ONBOARDING_INITIAL_GRANT_FAILED", get_client_ip(), user=actor_value(actor, "username"), success=False, ua=get_ua(), detail=str(exc))
                initial_grants = {"created_count": 0, "error": str(exc)}
            try:
                bonus = maybe_award_signup_bonus_for_actor(actor)
            except Exception as exc:
                audit("POINTS_WALLET_ONBOARDING_BONUS_FAILED", get_client_ip(), user=actor_value(actor, "username"), success=False, ua=get_ua(), detail=str(exc))
                bonus = {"created": False, "error": str(exc)}
        conn = points_service.get_db()
        try:
            points_service.ensure_schema(conn)
            status = wallet_onboarding_status(conn, points_service=points_service, user_id=actor["id"])
            conn.commit()
        finally:
            conn.close()
        audit("POINTS_WALLET_ONBOARDING_COMPLETED", get_client_ip(), user=actor_value(actor, "username"), success=True, ua=get_ua(), detail=f"mode={mode},address={(identity or {}).get('address')}")
        return json_resp({"ok": True, "wallet_identity": identity, "creation_fee": creation_fee, "initial_grants": initial_grants, "signup_bonus": bonus, "onboarding": status})

    @app.route("/api/points/wallet/onboarding", methods=["DELETE"])
    @require_csrf
    def points_wallet_onboarding_delete():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            data = {}
        conn = points_service.get_db()
        try:
            points_service.ensure_schema(conn)
            identity = delete_cold_wallet(
                conn,
                user_id=actor["id"],
                address=data.get("address") or "",
                reason=data.get("reason") or "user_deleted_cold_wallet",
            )
            points_service._rebuild_wallets_from_ledger(conn)
            status = wallet_onboarding_status(conn, points_service=points_service, user_id=actor["id"])
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            conn.close()
            return service_error(exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass
        audit(
            "POINTS_WALLET_COLD_DELETED",
            get_client_ip(),
            user=actor_value(actor, "username"),
            success=True,
            ua=get_ua(),
            detail=f"address={(identity or {}).get('address')}",
        )
        return json_resp({"ok": True, "wallet_identity": identity, "onboarding": status})

    @app.route("/api/root/points/system-wallets", methods=["GET"])
    @require_csrf_safe
    def root_points_system_wallets():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        conn = points_service.get_db()
        try:
            points_service.ensure_schema(conn)
            wallets = ensure_system_wallets(conn, chain_secret=getattr(points_service, "chain_secret", ""))
            conn.commit()
            return json_resp({"ok": True, "system_wallets": wallets})
        finally:
            conn.close()

    @app.route("/api/root/points/deposits/confirm", methods=["POST"])
    @require_csrf
    def root_points_confirm_deposit():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.confirm_deposit_to_hot_wallet(
                actor=actor,
                user_id=data.get("user_id"),
                source_address=data.get("source_address") or "",
                destination_address=data.get("destination_address") or "",
                amount_points=data.get("amount_points"),
                chain_tx_hash=data.get("chain_tx_hash") or "",
                chain=data.get("chain") or "points_chain_sim",
                confirmations=data.get("confirmations") or 20,
                required_confirmations=data.get("required_confirmations") or 20,
                risk_status=data.get("risk_status") or "accepted",
                metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else {},
            )
            audit(
                "POINTS_DEPOSIT_BRIDGE_CONFIRMED",
                get_client_ip(),
                user=actor_value(actor, "username"),
                success=True,
                ua=get_ua(),
                detail=f"user_id={data.get('user_id')}, amount={data.get('amount_points')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/ledger", methods=["GET"])
    @require_csrf_safe
    def points_ledger():
        actor, err = actor_or_401()
        if err:
            return err
        limit = parse_positive_int(request.args.get("limit"), default=50, maximum=200) or 50
        offset = max(0, int(request.args.get("offset") or 0))
        return json_resp({
            "ok": True,
            "ledger": points_service.list_ledger(user_id=actor["id"], limit=limit, offset=offset),
        })

    @app.route("/api/points/explorer/search", methods=["GET"])
    def points_explorer_search():
        if not points_chain_enabled():
            return points_chain_disabled_response()
        query = str(request.args.get("q") or "").strip()
        if not query:
            return json_resp({"ok": False, "msg": "請輸入交易 hash、Ledger UUID、錢包地址或區塊"}, 400)
        limit = parse_positive_int(request.args.get("limit"), default=25, maximum=100) or 25
        try:
            result = points_service.explorer_lookup(query, limit=limit)
        except Exception as exc:
            return service_error(exc)
        if not result:
            return json_resp({"ok": False, "msg": "查無鏈上資料"}), 404
        return json_resp({"ok": True, "result": result})

    @app.route("/api/points/explorer/tx/<path:ledger_ref>", methods=["GET"])
    def points_explorer_tx(ledger_ref):
        if not points_chain_enabled():
            return points_chain_disabled_response()
        result = points_service.explorer_transaction(ledger_ref)
        if not result:
            return json_resp({"ok": False, "msg": "找不到交易"}), 404
        return json_resp({"ok": True, "result": result})

    @app.route("/api/points/explorer/wallet/<path:address>", methods=["GET"])
    def points_explorer_wallet(address):
        if not points_chain_enabled():
            return points_chain_disabled_response()
        limit = parse_positive_int(request.args.get("limit"), default=25, maximum=100) or 25
        try:
            result = points_service.explorer_wallet(address, limit=limit)
        except Exception as exc:
            return service_error(exc)
        return json_resp({"ok": True, "result": result})

    @app.route("/api/points/explorer/block/<path:block_ref>", methods=["GET"])
    def points_explorer_block(block_ref):
        if not points_chain_enabled():
            return points_chain_disabled_response()
        result = points_service.explorer_block(block_ref)
        if not result:
            return json_resp({"ok": False, "msg": "找不到區塊"}), 404
        return json_resp({"ok": True, "result": result})

    @app.route("/api/points/explorer/bridge/<path:bridge_ref>", methods=["GET"])
    def points_explorer_bridge(bridge_ref):
        if not points_chain_enabled():
            return points_chain_disabled_response()
        result = points_service.explorer_bridge_event(bridge_ref)
        if not result:
            return json_resp({"ok": False, "msg": "找不到橋接事件"}), 404
        return json_resp({"ok": True, "result": result})

    @app.route("/api/points/explorer/fee-estimate", methods=["GET"])
    def points_explorer_fee_estimate():
        if not points_chain_enabled():
            return points_chain_disabled_response()
        try:
            fee_points = int(request.args.get("fee_points") or 0)
        except Exception:
            fee_points = 0
        fee_points = max(0, min(10000, fee_points))
        try:
            return json_resp({"ok": True, "estimate": points_service.explorer_fee_estimate(fee_points=fee_points)})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/explorer/accelerate", methods=["POST"])
    @require_csrf
    def points_explorer_accelerate():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        ledger_ref = str(data.get("ledger_ref") or data.get("ledger_uuid") or data.get("ledger_hash") or "").strip()
        fee_points = parse_positive_int(data.get("fee_points"), default=None, maximum=10000)
        request_uuid = str(data.get("request_uuid") or "").strip()
        if not ledger_ref:
            return json_resp({"ok": False, "msg": "ledger_ref required"}), 400
        if fee_points is None:
            return json_resp({"ok": False, "msg": "fee_points must be 1-10000"}), 400
        try:
            result = points_service.accelerate_explorer_transaction(
                actor=actor,
                ledger_ref=ledger_ref,
                fee_points=fee_points,
                request_uuid=request_uuid,
            )
            audit(
                "POINTS_CHAIN_TX_ACCELERATED",
                get_client_ip(),
                user=actor_value(actor, "username"),
                success=True,
                ua=get_ua(),
                detail=f"ledger_ref={ledger_ref},fee={fee_points}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/transactions/submit", methods=["POST"])
    @require_csrf
    def points_submit_transaction():
        actor, err = actor_or_401()
        if err:
            return err
        restricted = restriction_guard(actor, "wallet_transfer")
        if restricted:
            return restricted
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.submit_wallet_transaction(
                actor=actor,
                source_wallet_address=data.get("source_wallet_address") or data.get("from") or "",
                destination_wallet_address=data.get("destination_wallet_address") or data.get("to") or "",
                amount_points=data.get("amount_points") or data.get("value") or 0,
                fee_points=data.get("fee_points"),
                request_uuid=str(data.get("request_uuid") or "").strip() or None,
                memo=str(data.get("memo") or ""),
                signature=data.get("signature") or data.get("wallet_signature") or "",
            )
            audit(
                "POINTS_CHAIN_TX_SUBMITTED",
                get_client_ip(),
                user=actor_value(actor, "username"),
                success=True,
                ua=get_ua(),
                detail=f"tx_group_hash={result.get('tx_group_hash')}",
            )
            compact = (
                bool(data.get("compact"))
                or str(request.args.get("compact") or "").strip().lower() in {"1", "true", "yes", "on"}
            )
            if compact:
                transaction = result.get("transaction") if isinstance(result.get("transaction"), dict) else {}
                return json_resp({
                    "ok": bool(result.get("ok", True)),
                    "created": bool(result.get("created")),
                    "tx_group_hash": result.get("tx_group_hash") or transaction.get("tx_group_hash") or transaction.get("transaction_hash") or "",
                    "transaction_hash": result.get("transaction_hash") or result.get("tx_group_hash") or transaction.get("transaction_hash") or "",
                    "request_uuid": transaction.get("request_uuid") or data.get("request_uuid") or "",
                    "transaction_status": transaction.get("status") or ("pending" if result.get("created") else ""),
                    "settlement_rail": transaction.get("settlement_rail") or "",
                    "chain_required": transaction.get("chain_required"),
                    "compact": True,
                })
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/wallet/export.csv", methods=["GET"])
    @require_csrf_safe
    def points_wallet_export_csv():
        actor, err = actor_or_401()
        if err:
            return err
        user_id = int(actor["id"])
        wallet = points_service.get_wallet(user_id) or {}
        ledger_rows = load_user_ledger_for_export(user_id)
        rows = [{
            "record_type": "wallet_summary",
            "user_id": wallet.get("user_id", user_id),
            "public_account_id": wallet.get("public_account_id", ""),
            "currency_type": wallet.get("currency_type", DISPLAY_CURRENCY),
            "points_balance": wallet.get("points_balance", 0),
            "points_frozen": wallet.get("points_frozen", 0),
            "total_points_earned": wallet.get("total_points_earned", 0),
            "total_points_spent": wallet.get("total_points_spent", 0),
            "wallet_status": wallet.get("wallet_status", ""),
            "risk_level": wallet.get("risk_level", ""),
            "wallet_created_at": wallet.get("created_at", ""),
            "wallet_updated_at": wallet.get("updated_at", ""),
        }]
        for row in ledger_rows:
            rows.append({
                "record_type": "ledger",
                "user_id": user_id,
                "public_account_id": row.get("public_account_id", ""),
                "currency_type": row.get("currency_type", DISPLAY_CURRENCY),
                "ledger_uuid": row.get("ledger_uuid", ""),
                "direction": row.get("direction", ""),
                "amount": row.get("amount", ""),
                "balance_before": row.get("balance_before", ""),
                "balance_after": row.get("balance_after", ""),
                "action_type": row.get("action_type", ""),
                "reference_type": row.get("reference_type", ""),
                "reference_id": row.get("reference_id", ""),
                "reason": row.get("reason", ""),
                "ledger_hash": row.get("ledger_hash", ""),
                "previous_ledger_hash": row.get("previous_ledger_hash", ""),
                "chain_block_id": row.get("chain_block_id", ""),
                "status": row.get("status", ""),
                "created_at": row.get("created_at", ""),
            })
        fieldnames = [
            "record_type",
            "user_id",
            "public_account_id",
            "currency_type",
            "points_balance",
            "points_frozen",
            "total_points_earned",
            "total_points_spent",
            "wallet_status",
            "risk_level",
            "wallet_created_at",
            "wallet_updated_at",
            "ledger_uuid",
            "direction",
            "amount",
            "balance_before",
            "balance_after",
            "action_type",
            "reference_type",
            "reference_id",
            "reason",
            "ledger_hash",
            "previous_ledger_hash",
            "chain_block_id",
            "status",
            "created_at",
        ]
        audit("POINTS_WALLET_CSV_EXPORTED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"user_id={user_id},rows={len(rows)}")
        return csv_download_response(f"points_wallet_{actor['username']}.csv", fieldnames, rows)

    @app.route("/api/points/catalog", methods=["GET"])
    @require_csrf_safe
    def points_catalog():
        actor, err = actor_or_401()
        if err:
            return err
        return json_resp({"ok": True, "catalog": points_service.list_catalog()})

    @app.route("/api/root/economy/catalog", methods=["GET"])
    @require_csrf_safe
    def root_economy_catalog():
        actor, err = root_or_403()
        if err:
            return err
        category = str(request.args.get("category") or "").strip() or None
        return json_resp({
            "ok": True,
            "catalog": points_service.list_catalog(include_disabled=True, category=category),
        })

    @app.route("/api/root/economy/catalog", methods=["POST"])
    @require_csrf
    def root_economy_catalog_save():
        actor, err = root_or_403()
        if err:
            return err
        data, err = parse_json_body()
        if err:
            return err
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        try:
            item = points_service.upsert_catalog_item(
                actor=actor,
                item_key=data.get("item_key"),
                item_name=data.get("item_name"),
                category=data.get("category"),
                base_price=data.get("base_price"),
                dynamic_pricing=bool(data.get("dynamic_pricing")),
                min_price=data.get("min_price"),
                max_price=data.get("max_price"),
                enabled=data.get("enabled", True),
                metadata=metadata,
            )
            audit(
                "ECONOMY_PRICE_CATALOG_CHANGED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"item_key={item.get('item_key')}, category={item.get('category')}, price={item.get('base_price')}, enabled={item.get('enabled')}",
            )
            return json_resp({"ok": True, "item": item, "catalog": points_service.list_catalog(include_disabled=True)})
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/rules", methods=["GET"])
    @require_csrf_safe
    def points_rules():
        actor, err = actor_or_401()
        if err:
            return err
        rules = points_service.list_rules()
        return json_resp({"ok": True, "rules": [row for row in rules if row.get("enabled")]})

    @app.route("/api/points/transactions", methods=["GET"])
    @require_csrf_safe
    def points_transactions():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        limit = parse_positive_int(request.args.get("limit"), default=50, maximum=100) or 50
        compact_arg = str(request.args.get("compact") or "").strip().lower()
        if compact_arg in {"0", "false", "no", "off"}:
            compact = False
        else:
            compact = compact_arg in {"1", "true", "yes", "on"}
        cursor = request.args.get("cursor")
        maintenance_arg = str(request.args.get("sweep") or request.args.get("maintenance") or "").strip().lower()
        run_list_maintenance = actor_value(actor, "username") == "root" and maintenance_arg in {"1", "true", "yes", "on"}
        try:
            if compact:
                result = points_service.list_wallet_transactions_compact(
                    user_id=actor["id"],
                    limit=limit,
                    actor={"id": actor["id"], "username": actor["username"], "role": actor["role"]},
                    cursor=cursor,
                    run_root_sweep=run_list_maintenance,
                )
                metrics = result.pop("management_microbenchmark", {}) if isinstance(result, dict) else {}
                request.environ["hackme.sql_ms"] = metrics.get("sql_ms", 0)
                request.environ["hackme.python_aggregation_ms"] = metrics.get("python_aggregation_ms", 0)
            else:
                aggregation_started = time.perf_counter()
                result = points_service.list_wallet_transactions(
                    user_id=actor["id"],
                    limit=limit,
                    actor={"id": actor["id"], "username": actor["username"], "role": actor["role"]},
                    run_maintenance=run_list_maintenance,
                )
                request.environ["hackme.python_aggregation_ms"] = round((time.perf_counter() - aggregation_started) * 1000, 3)
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/governance/proposals", methods=["GET"])
    @require_csrf_safe
    def points_governance_proposals():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        limit = parse_positive_int(request.args.get("limit"), default=50, maximum=100) or 50
        try:
            return json_resp(points_service.list_governance_proposals(actor=actor, limit=limit))
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/governance/treasury-signer-center", methods=["GET"])
    @require_csrf_safe
    def admin_points_governance_treasury_signer_center():
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        limit = parse_positive_int(request.args.get("limit"), default=50, maximum=100) or 50
        try:
            return json_resp(points_service.official_treasury_signer_center(actor=actor, limit=limit))
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/governance/proposals/<path:proposal_uuid>/vote", methods=["POST"])
    @require_csrf
    def points_governance_vote(proposal_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.cast_governance_vote(
                actor=actor,
                proposal_uuid=str(proposal_uuid or "").strip(),
                vote=data.get("vote"),
                reason=data.get("reason") or "",
                recovery_choice=data.get("recovery_choice") or data.get("recovery_strategy") or "",
            )
            audit(
                "POINTS_CHAIN_GOVERNANCE_VOTE",
                get_client_ip(),
                user=actor_value(actor, "username"),
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={proposal_uuid},vote={data.get('vote')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/transactions/disputes", methods=["GET"])
    @require_csrf_safe
    def points_transaction_disputes():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        limit = parse_positive_int(request.args.get("limit"), default=50, maximum=100) or 50
        try:
            return json_resp(points_service.list_transaction_disputes(actor=actor, limit=limit))
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/transactions/disputes", methods=["POST"])
    @require_csrf
    def points_transaction_dispute_create():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        evidence = data.get("evidence")
        if isinstance(evidence, str):
            evidence = [item.strip() for item in evidence.splitlines() if item.strip()]
        try:
            result = points_service.create_transaction_dispute(
                actor=actor,
                tx_hash=data.get("tx_hash") or data.get("transaction_hash") or data.get("ledger_ref") or "",
                statement=data.get("statement") or data.get("reason") or "",
                victim_wallet_address=data.get("victim_wallet_address") or data.get("wallet_address") or "",
                claimed_amount_points=data.get("claimed_amount_points") or data.get("amount_points") or 0,
                loss_cause=data.get("loss_cause") or "unknown",
                evidence=evidence or [],
                public_key_jwk=data.get("public_key_jwk"),
                signature=data.get("signature") or "",
                signature_nonce=data.get("signature_nonce") or data.get("nonce") or "",
                from_wallet_address=data.get("from_wallet_address") or data.get("from") or "",
                to_wallet_address=data.get("to_wallet_address") or data.get("to") or "",
                chain_branch=data.get("chain_branch") or "",
                account_bound_proof=bool(data.get("account_bound_proof")),
            )
            audit(
                "POINTS_CHAIN_TX_DISPUTE_CREATED",
                "",
                user="address_proven_anonymous",
                success=True,
                ua=get_ua(),
                detail=f"dispute_uuid={result.get('dispute', {}).get('dispute_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/transactions/disputes/<path:dispute_uuid>/reply", methods=["POST"])
    @require_csrf
    def points_transaction_dispute_reply(dispute_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        evidence = data.get("evidence")
        if isinstance(evidence, str):
            evidence = [item.strip() for item in evidence.splitlines() if item.strip()]
        try:
            result = points_service.reply_transaction_dispute(
                actor=actor,
                dispute_uuid=str(dispute_uuid or "").strip(),
                statement=data.get("statement") or data.get("reason") or "",
                evidence=evidence or [],
                public_key_jwk=data.get("public_key_jwk"),
                signature=data.get("signature") or "",
                signature_nonce=data.get("signature_nonce") or data.get("nonce") or "",
                account_bound_proof=bool(data.get("account_bound_proof")),
            )
            audit(
                "POINTS_CHAIN_TX_DISPUTE_REPLY",
                "",
                user="address_proven_anonymous",
                success=True,
                ua=get_ua(),
                detail=f"dispute_uuid={result.get('dispute', {}).get('dispute_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/governance/public-proposal", methods=["POST"])
    @require_csrf
    def points_governance_public_proposal():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        evidence = data.get("evidence")
        if isinstance(evidence, str):
            evidence = [item.strip() for item in evidence.splitlines() if item.strip()]
        try:
            result = points_service.create_public_governance_proposal(
                actor=actor,
                action_type=data.get("action_type") or "",
                title=data.get("title") or "",
                reason=data.get("reason") or "",
                target_address=data.get("target_address") or data.get("wallet_address") or data.get("address") or "",
                incident_tx_hash=data.get("incident_tx_hash") or "",
                reference=data.get("reference") or "",
                evidence=evidence or [],
                proposal_severity=data.get("proposal_severity") or "NORMAL",
                description=data.get("description") or "",
                impact_scope=data.get("impact_scope") or "",
                risk_summary=data.get("risk_summary") or "",
                payload=data.get("payload") if isinstance(data.get("payload"), dict) else {},
            )
            audit(
                "POINTS_CHAIN_PUBLIC_GOVERNANCE_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')},action={data.get('action_type')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/governance/address-risk", methods=["POST"])
    @require_csrf
    def points_governance_address_risk():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        evidence = data.get("evidence")
        if isinstance(evidence, str):
            evidence = [item.strip() for item in evidence.splitlines() if item.strip()]
        try:
            result = points_service.create_address_risk_proposal(
                actor=actor,
                wallet_address=data.get("wallet_address") or data.get("address") or "",
                reason=data.get("reason") or "",
                evidence=evidence,
                reference=data.get("reference") or "",
            )
            audit(
                "POINTS_CHAIN_ADDRESS_RISK_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/governance/wallet-freeze", methods=["POST"])
    @require_csrf
    def points_governance_wallet_freeze():
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        evidence = data.get("evidence")
        if isinstance(evidence, str):
            evidence = [item.strip() for item in evidence.splitlines() if item.strip()]
        action = str(data.get("action") or "freeze").strip().lower()
        try:
            result = points_service.create_wallet_freeze_proposal(
                actor=actor,
                wallet_address=data.get("wallet_address") or data.get("address") or "",
                reason=data.get("reason") or "",
                evidence=evidence,
                reference=data.get("reference") or "",
                release=action in {"release", "unfreeze"},
            )
            audit(
                "POINTS_CHAIN_WALLET_FREEZE_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')},action={action}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/spend", methods=["POST"])
    @require_csrf
    def points_spend():
        actor, err = actor_or_401()
        if err:
            return err
        restricted = restriction_guard(actor, "service_spend")
        if restricted:
            return restricted
        data, err = parse_json_body()
        if err:
            return err
        item_key = str(data.get("item_key") or "").strip()
        if not item_key:
            return json_resp({"ok": False, "msg": "item_key required"}), 400
        quantity = parse_positive_int(data.get("quantity"), default=1, maximum=1000)
        if quantity is None:
            return json_resp({"ok": False, "msg": "quantity must be 1-1000"}), 400
        try:
            result = points_service.rc1_facade().spend_service_fee(
                user_id=actor["id"],
                item_key=item_key,
                quantity=quantity,
                reference_type="price_catalog",
                reference_id=f"catalog:{item_key}",
                idempotency_key=_stable_spend_key(user_id=actor["id"], item_key=item_key, quantity=quantity),
                metadata={},
                actor=actor,
                source_wallet_address=data.get("source_wallet_address") or "",
                request_uuid=data.get("request_uuid") or data.get("charge_uuid") or "",
                signature=data.get("signature") or data.get("wallet_signature") or "",
                chain_enabled=points_chain_enabled(),
            )
            audit("POINTS_SPEND", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"item_key={item_key}, quantity={quantity}")
            return json_resp(result)
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/points/ledger/<ledger_uuid>/proof", methods=["GET"])
    @require_csrf_safe
    def points_ledger_proof(ledger_uuid):
        actor, err = actor_or_401()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        proof = points_service.ledger_proof(ledger_uuid)
        if not proof:
            return json_resp({"ok": False, "msg": "找不到 ledger"}), 404
        proof_account = proof.get("public_account_id") or (proof.get("ledger") or {}).get("public_account_id")
        admin_role = "super_admin" if actor_value(actor, "username") == "root" else actor_value(actor, "role", "user")
        is_manager = role_rank(admin_role) >= role_rank("manager")
        if not proof_account and not is_manager:
            return json_resp({"ok": False, "msg": "權限不足"}), 403
        if proof_account and proof_account != points_service.get_wallet(actor["id"])["public_account_id"] and not is_manager:
            return json_resp({"ok": False, "msg": "權限不足"}), 403
        return json_resp({"ok": True, "proof": proof})

    @app.route("/api/admin/points/wallets/<int:user_id>", methods=["GET"])
    @require_csrf_safe
    def admin_points_wallet(user_id):
        return json_resp({
            "ok": False,
            "code": "blockchain_permission_model",
            "msg": "私有鏈模式已停用依帳號查詢用戶錢包；請改用公開 Explorer 查交易 hash 或錢包地址。",
        }, 410)

    @app.route("/api/root/points/wallets/<int:user_id>/sanction", methods=["POST"])
    @require_csrf
    def root_points_wallet_sanction(user_id):
        return json_resp({
            "ok": False,
            "code": "blockchain_permission_model",
            "msg": "私有鏈模式不允許 root 直接處分用戶錢包；請改用治理投票的地址凍結、詐騙標記或緊急分支流程。",
        }, 410)

    @app.route("/api/admin/points/ledger", methods=["GET"])
    @require_csrf_safe
    def admin_points_ledger():
        return json_resp({
            "ok": False,
            "code": "blockchain_permission_model",
            "msg": "私有鏈模式已停用後台依會員列帳；請用公開 Explorer 以交易 hash、地址或區塊查詢。",
        }, 410)

    @app.route("/api/admin/points/adjust", methods=["POST"])
    @require_csrf
    def admin_points_adjust():
        return json_resp({
            "ok": False,
            "code": "blockchain_permission_model",
            "msg": "私有鏈模式已停用手動加減積分；官方撥款需改走治理提案與官方多簽，用戶扣款需由用戶授權交易觸發。",
        }, 410)

    @app.route("/api/root/points/official-wallet/grant", methods=["POST"])
    @require_csrf
    def root_points_official_wallet_grant():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.create_treasury_transfer_proposal(
                actor=actor,
                destination_wallet_address=data.get("destination_wallet_address") or data.get("to") or "",
                amount=data.get("amount") or data.get("amount_points") or 0,
                reason=data.get("reason") or "",
                reference=str(data.get("reference") or data.get("request_uuid") or "").strip(),
                action_type=data.get("action_type") or "TREASURY_TRANSFER",
                memo=data.get("memo") or data.get("reason") or "",
            )
            audit(
                "OFFICIAL_WALLET_GRANT_PROPOSAL_SUBMITTED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "code": "blockchain_permission_model", "msg": str(exc)}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/governance/treasury-transfer", methods=["POST"])
    @require_csrf
    def admin_points_governance_treasury_transfer():
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.create_treasury_transfer_proposal(
                actor=actor,
                destination_wallet_address=data.get("destination_wallet_address") or data.get("to") or "",
                amount=data.get("amount") or data.get("amount_points") or 0,
                reason=data.get("reason") or "",
                reference=data.get("reference") or "",
                action_type=data.get("action_type") or "TREASURY_TRANSFER",
                memo=data.get("memo") or "",
            )
            audit(
                "POINTS_CHAIN_TREASURY_TRANSFER_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/governance/mint-request", methods=["POST"])
    @require_csrf
    def admin_points_governance_mint_request():
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.create_mint_request_proposal(
                actor=actor,
                amount=data.get("amount") or data.get("amount_points") or 0,
                reason=data.get("reason") or "",
                destination_fund_key=data.get("destination_fund_key") or "official_treasury",
                reference=data.get("reference") or "",
            )
            audit(
                "POINTS_CHAIN_MINT_REQUEST_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/governance/supply-expansion", methods=["POST"])
    @require_csrf
    def admin_points_governance_supply_expansion():
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.create_supply_expansion_request_proposal(
                actor=actor,
                requested_delta=data.get("requested_delta") or data.get("amount") or data.get("amount_points") or 0,
                reason=data.get("reason") or "",
                destination_fund_key=data.get("destination_fund_key") or "official_treasury",
                reference=data.get("reference") or "",
                financial_report=data.get("financial_report") or "",
                risk_disclosure=data.get("risk_disclosure") or "",
            )
            audit(
                "POINTS_CHAIN_SUPPLY_EXPANSION_REQUEST_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/governance/policy", methods=["POST"])
    @require_csrf
    def admin_points_governance_policy():
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {
                "parameter_key": data.get("parameter_key") or "",
                "parameter_value": data.get("parameter_value") or "",
                "feature_key": data.get("feature_key") or "",
                "burn_policy": data.get("burn_policy") or "",
                "lockdown_scope": data.get("lockdown_scope") or "",
                "description": data.get("description") or "",
            }
        try:
            result = points_service.create_policy_governance_proposal(
                actor=actor,
                action_type=data.get("action_type") or "",
                title=data.get("title") or "",
                reason=data.get("reason") or "",
                payload=payload,
                reference=data.get("reference") or "",
                proposal_severity=data.get("proposal_severity") or "NORMAL",
                impact_scope=data.get("impact_scope") or "",
                risk_summary=data.get("risk_summary") or "",
            )
            audit(
                "POINTS_CHAIN_POLICY_GOVERNANCE_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')},action={data.get('action_type')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/points/governance/address-risk", methods=["POST"])
    @require_csrf
    def root_points_governance_address_risk():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        evidence = data.get("evidence")
        if isinstance(evidence, str):
            evidence = [item.strip() for item in evidence.splitlines() if item.strip()]
        try:
            result = points_service.create_address_risk_proposal(
                actor=actor,
                wallet_address=data.get("wallet_address") or data.get("address") or "",
                reason=data.get("reason") or "",
                evidence=evidence,
                reference=data.get("reference") or "",
            )
            audit(
                "POINTS_CHAIN_ADDRESS_RISK_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/points/governance/recovery-branch", methods=["POST"])
    @require_csrf
    def root_points_governance_recovery_branch():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        excluded = data.get("excluded_tx_hashes")
        if isinstance(excluded, str):
            excluded = [item.strip() for item in excluded.splitlines() if item.strip()]
        incident_refs = data.get("incident_tx_hashes")
        if isinstance(incident_refs, str):
            incident_refs = [item.strip() for item in incident_refs.splitlines() if item.strip()]
        try:
            result = points_service.create_emergency_recovery_branch_proposal(
                actor=actor,
                incident_tx_hash=data.get("incident_tx_hash") or "",
                reason=data.get("reason") or "",
                base_block_number=data.get("base_block_number") or None,
                base_block_hash=data.get("base_block_hash") or "",
                excluded_tx_hashes=excluded or [],
                recovery_strategy=data.get("recovery_strategy") or data.get("strategy") or "treasury_compensation",
                loss_cause=data.get("loss_cause") or "protocol_fault",
                compensation_rate_per_10000=data.get("compensation_rate_per_10000"),
                victim_statement=data.get("victim_statement") or "",
                victim_evidence_refs=data.get("victim_evidence_refs") or data.get("evidence_refs") or [],
                victim_claims=data.get("victim_claims") or [],
                incident_tx_hashes=incident_refs or [],
                reference=data.get("reference") or "",
            )
            audit(
                "POINTS_CHAIN_RECOVERY_BRANCH_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/governance/recovery-branch", methods=["POST"])
    @require_csrf
    def admin_points_governance_recovery_branch():
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        excluded = data.get("excluded_tx_hashes")
        if isinstance(excluded, str):
            excluded = [item.strip() for item in excluded.splitlines() if item.strip()]
        incident_refs = data.get("incident_tx_hashes")
        if isinstance(incident_refs, str):
            incident_refs = [item.strip() for item in incident_refs.splitlines() if item.strip()]
        try:
            result = points_service.create_emergency_recovery_branch_proposal(
                actor=actor,
                incident_tx_hash=data.get("incident_tx_hash") or "",
                reason=data.get("reason") or "",
                base_block_number=data.get("base_block_number") or None,
                base_block_hash=data.get("base_block_hash") or "",
                excluded_tx_hashes=excluded or [],
                recovery_strategy=data.get("recovery_strategy") or data.get("strategy") or "treasury_compensation",
                loss_cause=data.get("loss_cause") or "protocol_fault",
                compensation_rate_per_10000=data.get("compensation_rate_per_10000"),
                victim_statement=data.get("victim_statement") or "",
                victim_evidence_refs=data.get("victim_evidence_refs") or data.get("evidence_refs") or [],
                victim_claims=data.get("victim_claims") or [],
                incident_tx_hashes=incident_refs or [],
                reference=data.get("reference") or "",
            )
            audit(
                "POINTS_CHAIN_RECOVERY_BRANCH_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/transactions/disputes/<path:dispute_uuid>/review", methods=["POST"])
    @require_csrf
    def admin_points_transaction_dispute_review(dispute_uuid):
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.review_transaction_dispute(
                actor=actor,
                dispute_uuid=str(dispute_uuid or "").strip(),
                status=data.get("status") or "",
                review_note=data.get("review_note") or data.get("reason") or "",
                recommended_strategy=data.get("recommended_strategy") or "tainted_remainder_return",
                create_proposal=bool(data.get("create_proposal")),
            )
            audit(
                "POINTS_CHAIN_TX_DISPUTE_REVIEWED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"dispute_uuid={dispute_uuid},status={data.get('status')},proposal={(result.get('proposal') or {}).get('proposal_uuid')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/points/governance/wallet-freeze", methods=["POST"])
    @require_csrf
    def root_points_governance_wallet_freeze():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        evidence = data.get("evidence")
        if isinstance(evidence, str):
            evidence = [item.strip() for item in evidence.splitlines() if item.strip()]
        action = str(data.get("action") or "freeze").strip().lower()
        try:
            result = points_service.create_wallet_freeze_proposal(
                actor=actor,
                wallet_address=data.get("wallet_address") or data.get("address") or "",
                reason=data.get("reason") or "",
                evidence=evidence,
                reference=data.get("reference") or "",
                release=action in {"release", "unfreeze"},
            )
            audit(
                "POINTS_CHAIN_WALLET_FREEZE_PROPOSAL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={result.get('proposal', {}).get('proposal_uuid')},action={action}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/points/governance/proposals/<path:proposal_uuid>/execute", methods=["POST"])
    @require_csrf
    def root_points_governance_execute(proposal_uuid):
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        try:
            result = points_service.execute_governance_proposal(
                actor=actor,
                proposal_uuid=str(proposal_uuid or "").strip(),
            )
            audit(
                "POINTS_CHAIN_GOVERNANCE_EXECUTE",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={proposal_uuid},action={result.get('result', {}).get('action')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/governance/proposals/<path:proposal_uuid>/execute", methods=["POST"])
    @require_csrf
    def admin_points_governance_execute(proposal_uuid):
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        try:
            result = points_service.execute_governance_proposal(
                actor=actor,
                proposal_uuid=str(proposal_uuid or "").strip(),
            )
            audit(
                "POINTS_CHAIN_GOVERNANCE_EXECUTE",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={proposal_uuid},action={result.get('result', {}).get('action')}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/governance/proposals/<path:proposal_uuid>/sponsor", methods=["POST"])
    @require_csrf
    def admin_points_governance_sponsor(proposal_uuid):
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        try:
            result = points_service.sponsor_governance_proposal(
                actor=actor,
                proposal_uuid=str(proposal_uuid or "").strip(),
            )
            audit(
                "POINTS_CHAIN_GOVERNANCE_SPONSOR",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={proposal_uuid}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/governance/proposals/<path:proposal_uuid>/cancel", methods=["POST"])
    @require_csrf
    def admin_points_governance_cancel(proposal_uuid):
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.cancel_governance_proposal(
                actor=actor,
                proposal_uuid=str(proposal_uuid or "").strip(),
                reason=data.get("reason") or "",
            )
            audit(
                "POINTS_CHAIN_GOVERNANCE_CANCEL",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={proposal_uuid}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/governance/proposals/<path:proposal_uuid>/multisig-sign", methods=["POST"])
    @require_csrf
    def admin_points_governance_multisig_sign(proposal_uuid):
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.sign_governance_multisig(
                actor=actor,
                proposal_uuid=str(proposal_uuid or "").strip(),
                signer_wallet_address=data.get("signer_wallet_address") or data.get("wallet_address") or "",
                signature=data.get("signature") or "",
            )
            audit(
                "POINTS_CHAIN_GOVERNANCE_MULTISIG_SIGN",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={proposal_uuid}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/root/points/governance/proposals/<path:proposal_uuid>/veto", methods=["POST"])
    @require_csrf
    def root_points_governance_veto(proposal_uuid):
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        try:
            result = points_service.veto_governance_proposal(
                actor=actor,
                proposal_uuid=str(proposal_uuid or "").strip(),
                reason=data.get("reason") or "",
            )
            audit(
                "POINTS_CHAIN_GOVERNANCE_VETO",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"proposal_uuid={proposal_uuid}",
            )
            return json_resp(result)
        except PermissionError as exc:
            return json_resp({"ok": False, "msg": str(exc) or "權限不足"}), 403
        except Exception as exc:
            return service_error(exc)

    @app.route("/api/admin/points/pending-rewards", methods=["GET", "POST"])
    @require_csrf_safe
    def admin_points_pending_rewards():
        return json_resp({
            "ok": False,
            "code": "blockchain_permission_model",
            "msg": "私有鏈模式已停用後台待審加點；官方撥款需改走治理提案與官方多簽。",
        }, 410)

    @app.route("/api/admin/points/pending-rewards/<int:pending_reward_id>/review", methods=["POST"])
    @require_csrf
    def admin_points_pending_reward_review(pending_reward_id):
        return json_resp({
            "ok": False,
            "code": "blockchain_permission_model",
            "msg": "私有鏈模式已停用後台待審加點；官方撥款需改走治理提案與官方多簽。",
        }, 410)

    @app.route("/api/root/points/chain/seal", methods=["POST"])
    @require_csrf
    def root_points_chain_seal():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data = {}
        if request.is_json:
            data = request.get_json(silent=True) or {}
        limit = parse_positive_int(data.get("limit"), default=100, maximum=500) or 100
        actor_snapshot = management_actor(actor)

        def worker(progress):
            progress(stage="seal_block", progress_percent=20, detail=f"sealing up to {limit} ledger entries")
            result = points_service.seal_block(actor=actor_snapshot, limit=limit)
            progress(stage="snapshot", progress_percent=90, detail="recording seal result snapshot")
            return {"ok": bool(result.get("ok", True)), "seal": result}

        def summary(payload):
            seal = payload.get("seal") if isinstance(payload.get("seal"), dict) else {}
            return {
                "ok": bool(seal.get("ok", payload.get("ok", True))),
                "snapshot_key": "points_chain_seal",
                "sealed": bool(seal.get("sealed") or seal.get("created")),
                "block": seal.get("block"),
                "msg": seal.get("msg"),
            }

        sql_started = time.perf_counter()
        started = start_management_plane_job(
            get_db=get_db,
            actor=actor_snapshot,
            job_type="points_chain_seal",
            title="PointsChain seal",
            snapshot_key="points_chain_seal",
            request_payload={"limit": limit},
            worker=worker,
            summary_builder=summary,
            queue_class="points_chain_admin",
            resource_locks=("finance_db",),
        )
        request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        audit("POINTS_CHAIN_SEAL_QUEUED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"job_uuid={started['job'].get('job_uuid')},limit={limit}")
        return management_job_response(started, snapshot_key="points_chain_seal")

    @app.route("/api/root/points/chain/seal/latest", methods=["GET"])
    @require_csrf_safe
    def root_points_chain_seal_latest():
        actor, err = root_or_403()
        if err:
            return err
        return management_snapshot_response("points_chain_seal")

    @app.route("/api/root/points/chain/seal/jobs/<path:job_uuid>", methods=["GET"])
    @require_csrf_safe
    def root_points_chain_seal_job(job_uuid):
        actor, err = root_or_403()
        if err:
            return err
        return management_job_status_response(job_uuid)

    def start_points_chain_verify_job(actor):
        actor_snapshot = management_actor(actor)

        def worker(progress):
            progress(stage="verify_chain", progress_percent=15, detail="verifying PointsChain off request path")
            result = points_service.verify_chain_bounded_snapshot()
            incident = None
            if not result.get("ok") and not result.get("bounded") and server_mode_service and hasattr(server_mode_service, "enter_incident_lockdown"):
                progress(stage="incident_lockdown", progress_percent=85, detail="verification failed; entering incident lockdown")
                try:
                    incident = server_mode_service.enter_incident_lockdown(
                        actor=actor_snapshot,
                        trigger_type="points_chain_verify_failed",
                        reason="PointsChain verification failed",
                        verification=result,
                    )
                except Exception as exc:
                    incident = {"ok": False, "msg": str(exc)}
            progress(stage="snapshot", progress_percent=90, detail="recording verification snapshot")
            return {"ok": bool(result.get("ok")), "verification": result, "incident": incident}

        def summary(payload):
            verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
            return {
                "ok": bool(payload.get("ok")),
                "snapshot_key": "points_chain_verify",
                "verification_ok": bool(verification.get("ok")),
                "error_count": len(verification.get("errors") or []),
                "counts": verification.get("counts") or {},
                "financial_ok": verification.get("financial_ok"),
                "incident": payload.get("incident"),
            }

        sql_started = time.perf_counter()
        started = start_management_plane_job(
            get_db=get_db,
            actor=actor_snapshot,
            job_type="points_chain_verify",
            title="PointsChain verify",
            snapshot_key="points_chain_verify",
            request_payload={},
            worker=worker,
            summary_builder=summary,
            reuse_recent_success_seconds=10,
            queue_class="points_chain_admin",
            resource_locks=("finance_db",),
        )
        request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        return started

    @app.route("/api/root/points/chain/verify", methods=["GET"])
    @require_csrf_safe
    def root_points_chain_verify():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        started = start_points_chain_verify_job(actor)
        return management_job_response(started, snapshot_key="points_chain_verify")

    @app.route("/api/root/points/chain/verify/jobs", methods=["POST"])
    @require_csrf
    def root_points_chain_verify_start_job():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        started = start_points_chain_verify_job(actor)
        return management_job_response(started, snapshot_key="points_chain_verify")

    @app.route("/api/root/points/chain/verify/latest", methods=["GET"])
    @require_csrf_safe
    def root_points_chain_verify_latest():
        actor, err = root_or_403()
        if err:
            return err
        return management_snapshot_response("points_chain_verify")

    @app.route("/api/root/points/chain/verify/jobs/<path:job_uuid>", methods=["GET"])
    @require_csrf_safe
    def root_points_chain_verify_job(job_uuid):
        actor, err = root_or_403()
        if err:
            return err
        return management_job_status_response(job_uuid)

    @app.route("/api/root/points/chain/recovery", methods=["GET"])
    @require_csrf_safe
    def root_points_chain_recovery():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        return json_resp({
            "ok": True,
            "recovery": points_service.safe_mode_status(),
            "backups": [],
            "restore_disabled": True,
            "recovery_policy": "branch_governance_forensic_only",
            "msg": "PointsChain 不提供 ledger backup 還原；異常處理須透過 safe mode、forensic bundle、分支與緊急治理保留事件軌跡。",
        })

    def start_points_chain_recovery_auto_handle_job(actor):
        actor_snapshot = management_actor(actor)

        def worker(progress):
            progress(stage="bounded_verify", progress_percent=15, detail="building bounded PointsChain verification before recovery guidance")
            verification = points_service.verify_chain_bounded_snapshot()
            recovery = points_service.safe_mode_status()
            if verification.get("ok") and not recovery.get("safe_mode"):
                progress(stage="verified_clean", progress_percent=90, detail="PointsChain is clean; no recovery action needed")
                return {
                    "ok": True,
                    "action": "verified_clean",
                    "msg": "PointsChain 驗證正常，無需恢復",
                    "verification": verification,
                    "recovery": recovery,
                    "restore_disabled": True,
                    "recovery_policy": "branch_governance_forensic_only",
                }
            progress(stage="governance_required", progress_percent=90, detail="recovery requires branch/governance handling")
            return {
                "ok": False,
                "action": "branch_governance_required",
                "msg": "PointsChain 異常已停止自動覆寫還原；請檢查 forensic bundle，必要時建立 recovery branch 或發起緊急治理修正交易。",
                "verification": verification,
                "recovery": recovery,
                "restore_disabled": True,
                "recovery_policy": "branch_governance_forensic_only",
                "backups": [],
            }

        def summary(payload):
            verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
            recovery = payload.get("recovery") if isinstance(payload.get("recovery"), dict) else {}
            return {
                "ok": bool(payload.get("ok")),
                "snapshot_key": "points_chain_recovery_auto_handle",
                "action": payload.get("action"),
                "verification_ok": bool(verification.get("ok")),
                "safe_mode": bool(recovery.get("safe_mode")),
                "error_count": len(verification.get("errors") or []),
                "restore_disabled": True,
            }

        sql_started = time.perf_counter()
        started = start_management_plane_job(
            get_db=get_db,
            actor=actor_snapshot,
            job_type="points_chain_recovery_auto_handle",
            title="PointsChain recovery auto-handle",
            snapshot_key="points_chain_recovery_auto_handle",
            request_payload={"restore_disabled": True},
            worker=worker,
            summary_builder=summary,
            queue_class="points_chain_admin",
            resource_locks=("finance_db",),
        )
        request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        return started

    @app.route("/api/root/points/chain/recovery/auto-handle", methods=["POST"])
    @require_csrf
    def root_points_chain_recovery_auto_handle():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data, err = parse_json_body()
        if err:
            return err
        if str(data.get("confirm") or "") != "AUTO HANDLE POINTSCHAIN":
            return json_resp({"ok": False, "msg": "confirm 欄位必須為 AUTO HANDLE POINTSCHAIN"}, 400)
        try:
            audit(
                "POINTS_CHAIN_AUTO_HANDLE_START",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail="root requested one-click PointsChain anomaly handling",
            )
            started = start_points_chain_recovery_auto_handle_job(actor)
            audit(
                "POINTS_CHAIN_AUTO_HANDLE_QUEUED",
                get_client_ip(),
                user=actor["username"],
                success=True,
                ua=get_ua(),
                detail=f"job_uuid={started['job'].get('job_uuid')}, backup_restore_disabled=true",
            )
            return management_job_response(started, snapshot_key="points_chain_recovery_auto_handle")
        except Exception as exc:
            audit(
                "POINTS_CHAIN_AUTO_HANDLE_FAILED",
                get_client_ip(),
                user=actor["username"],
                success=False,
                ua=get_ua(),
                detail=str(exc),
            )
            return service_error(exc)

    @app.route("/api/root/points/chain/recovery/auto-handle/latest", methods=["GET"])
    @require_csrf_safe
    def root_points_chain_recovery_auto_handle_latest():
        actor, err = root_or_403()
        if err:
            return err
        return management_snapshot_response("points_chain_recovery_auto_handle")

    @app.route("/api/root/points/chain/recovery/auto-handle/jobs/<path:job_uuid>", methods=["GET"])
    @require_csrf_safe
    def root_points_chain_recovery_auto_handle_job(job_uuid):
        actor, err = root_or_403()
        if err:
            return err
        return management_job_status_response(job_uuid)

    @app.route("/api/root/points/chain/backups", methods=["POST"])
    @require_csrf
    def root_points_chain_backup():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        audit(
            "POINTS_CHAIN_BACKUP_DISABLED",
            get_client_ip(),
            user=actor["username"],
            success=False,
            ua=get_ua(),
            detail="ledger backup/restore disabled; use branch/governance recovery",
        )
        return json_resp({
            "ok": False,
            "disabled": True,
            "msg": "PointsChain 不允許建立可還原的 ledger backup；請使用全站 snapshot 做伺服器災難備援，鏈異常則用分支、safe mode、forensic bundle 與緊急治理處理。",
        }, 410)

    @app.route("/api/root/points/chain/recovery/approve", methods=["POST"])
    @require_csrf
    def root_points_chain_recovery_approve():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        audit(
            "POINTS_CHAIN_RECOVERY_RESTORE_DISABLED",
            get_client_ip(),
            user=actor["username"],
            success=False,
            ua=get_ua(),
            detail="backup restore rejected; append-only chain recovery must use branches/governance",
        )
        return json_resp({
            "ok": False,
            "disabled": True,
            "msg": "備份還原會覆寫 append-only ledger，已停用。請透過 recovery branch、緊急治理、疑義交易與補正交易保留完整事件軌跡。",
            "recovery_policy": "branch_governance_forensic_only",
        }, 410)

    def start_root_points_report_job(actor):
        actor_snapshot = management_actor(actor)

        def worker(progress):
            progress(stage="root_report", progress_percent=15, detail="building PointsChain root report off request path")
            if hasattr(points_service, "root_report_bounded_snapshot"):
                report = points_service.root_report_bounded_snapshot()
            else:
                report = points_service.root_report()
            progress(stage="snapshot", progress_percent=90, detail="recording root report snapshot")
            return {"ok": True, "report": report}

        def summary(payload):
            report = payload.get("report") if isinstance(payload.get("report"), dict) else {}
            verification = report.get("verification") if isinstance(report.get("verification"), dict) else {}
            stats = report.get("stats") if isinstance(report.get("stats"), dict) else {}
            return {
                "ok": bool(payload.get("ok", True)),
                "snapshot_key": "points_root_report",
                "verification_ok": bool(verification.get("ok")),
                "financial_ok": verification.get("financial_ok"),
                "ledger_count": ((stats.get("ledger") or {}).get("count") if isinstance(stats.get("ledger"), dict) else None),
                "management_timing": report.get("management_timing") or {},
            }

        sql_started = time.perf_counter()
        started = start_management_plane_job(
            get_db=get_db,
            actor=actor_snapshot,
            job_type="points_root_report",
            title="Points root report",
            snapshot_key="points_root_report",
            request_payload={},
            worker=worker,
            summary_builder=summary,
            reuse_recent_success_seconds=15,
            queue_class="points_chain_admin",
            resource_locks=("finance_db",),
        )
        request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        return started

    @app.route("/api/root/points/report", methods=["GET"])
    @require_csrf_safe
    def root_points_report():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        refresh = str(request.args.get("refresh") or request.args.get("start_job") or "").strip().lower() in {"1", "true", "yes", "on"}
        if not refresh:
            conn = get_db()
            try:
                snapshot = get_management_snapshot(conn, snapshot_key="points_root_report", include_payload=True)
            finally:
                conn.close()
            if snapshot.get("ok"):
                payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
                return json_resp({
                    "ok": True,
                    "report": payload.get("report") or {},
                    "snapshot": {
                        "snapshot_key": snapshot.get("snapshot_key"),
                        "generated_at": snapshot.get("generated_at"),
                        "source_job_uuid": snapshot.get("source_job_uuid"),
                        "summary": snapshot.get("summary") or {},
                        "snapshot_backed": True,
                    },
                })
        started = start_root_points_report_job(actor)
        return management_job_response(started, snapshot_key="points_root_report")

    @app.route("/api/root/points/report/jobs", methods=["POST"])
    @require_csrf
    def root_points_report_start_job():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        started = start_root_points_report_job(actor)
        return management_job_response(started, snapshot_key="points_root_report")

    @app.route("/api/root/points/report/latest", methods=["GET"])
    @require_csrf_safe
    def root_points_report_latest():
        actor, err = root_or_403()
        if err:
            return err
        return management_snapshot_response("points_root_report", payload_key="report")

    @app.route("/api/root/points/report/jobs/<path:job_uuid>", methods=["GET"])
    @require_csrf_safe
    def root_points_report_job(job_uuid):
        actor, err = root_or_403()
        if err:
            return err
        return management_job_status_response(job_uuid)

    def start_root_points_finality_sweep_job(actor, *, limit):
        actor_snapshot = management_actor(actor)
        sweep_limit = max(1, min(500, int(limit or 50)))

        def worker(progress):
            progress(stage="finality_sweep", progress_percent=20, detail=f"running bounded finality sweep limit={sweep_limit}")
            result = points_service.run_transfer_finality_sweep(actor=actor_snapshot, limit=sweep_limit)
            progress(stage="snapshot", progress_percent=90, detail="recording finality sweep snapshot")
            return {"ok": bool(result.get("ok", True)), "finality_sweep": result}

        def summary(payload):
            sweep_payload = payload.get("finality_sweep") if isinstance(payload.get("finality_sweep"), dict) else {}
            sweep = sweep_payload.get("sweep") if isinstance(sweep_payload.get("sweep"), dict) else {}
            deposit = sweep_payload.get("deposit_bridge") if isinstance(sweep_payload.get("deposit_bridge"), dict) else {}
            return {
                "ok": bool(sweep_payload.get("ok", payload.get("ok", True))),
                "snapshot_key": "points_finality_sweep",
                "limit": int(sweep_payload.get("limit") or sweep_limit),
                "finalized_count": int(sweep.get("finalized_count") or 0),
                "confirmed_count": int(sweep.get("confirmed_count") or 0),
                "failed_count": int(sweep.get("failed_count") or 0),
                "deposit_credited_count": int(deposit.get("credited_count") or 0),
                "finalization_paused": bool(sweep_payload.get("finalization_paused")),
                "management_timing": sweep_payload.get("management_timing") or {},
            }

        sql_started = time.perf_counter()
        started = start_management_plane_job(
            get_db=get_db,
            actor=actor_snapshot,
            job_type="points_finality_sweep",
            title="Points finality sweep",
            snapshot_key="points_finality_sweep",
            request_payload={"limit": sweep_limit},
            worker=worker,
            summary_builder=summary,
            reuse_recent_success_seconds=3,
            queue_class="points_chain_admin",
            resource_locks=("finance_db",),
        )
        request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        return started

    @app.route("/api/root/points/finality-sweep", methods=["POST"])
    @require_csrf
    def root_points_finality_sweep():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data = request.get_json(silent=True) if request.is_json else {}
        if not isinstance(data, dict):
            data = {}
        limit = parse_positive_int(data.get("limit") or request.args.get("limit"), default=50, maximum=500) or 50
        started = start_root_points_finality_sweep_job(actor, limit=limit)
        audit("POINTS_FINALITY_SWEEP_QUEUED", get_client_ip(), user=actor["username"], success=True, ua=get_ua(), detail=f"job_uuid={started['job'].get('job_uuid')},limit={limit}")
        return management_job_response(started, snapshot_key="points_finality_sweep")

    @app.route("/api/root/points/finality-sweep/jobs", methods=["POST"])
    @require_csrf
    def root_points_finality_sweep_start_job():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        data = request.get_json(silent=True) if request.is_json else {}
        if not isinstance(data, dict):
            data = {}
        limit = parse_positive_int(data.get("limit") or request.args.get("limit"), default=50, maximum=500) or 50
        started = start_root_points_finality_sweep_job(actor, limit=limit)
        return management_job_response(started, snapshot_key="points_finality_sweep")

    @app.route("/api/root/points/finality-sweep/latest", methods=["GET"])
    @require_csrf_safe
    def root_points_finality_sweep_latest():
        actor, err = root_or_403()
        if err:
            return err
        return management_snapshot_response("points_finality_sweep", payload_key="finality_sweep")

    @app.route("/api/root/points/finality-sweep/jobs/<path:job_uuid>", methods=["GET"])
    @require_csrf_safe
    def root_points_finality_sweep_job(job_uuid):
        actor, err = root_or_403()
        if err:
            return err
        return management_job_status_response(job_uuid)

    @app.route("/api/root/management/jobs/<path:job_uuid>", methods=["GET"])
    @require_csrf_safe
    def root_management_job_status(job_uuid):
        actor, err = root_or_403()
        if err:
            return err
        return management_job_status_response(job_uuid)

    @app.route("/api/root/management/snapshots/<path:snapshot_key>", methods=["GET"])
    @require_csrf_safe
    def root_management_snapshot(snapshot_key):
        actor, err = root_or_403()
        if err:
            return err
        missing_status = 200 if str(request.args.get("missing_ok") or "").strip() == "1" else 404
        return management_snapshot_response(snapshot_key, missing_status=missing_status)

    def start_root_points_financial_invariants_job(actor):
        actor_snapshot = management_actor(actor)

        def worker(progress):
            progress(stage="financial_invariants", progress_percent=15, detail="checking PointsChain financial invariants off request path")
            result = points_service.financial_invariant_report()
            progress(stage="snapshot", progress_percent=90, detail="recording financial invariant snapshot")
            return {"ok": bool(result.get("ok")), "financial_invariants": result}

        def summary(payload):
            invariants = payload.get("financial_invariants") if isinstance(payload.get("financial_invariants"), dict) else {}
            return {
                "ok": bool(invariants.get("ok")),
                "snapshot_key": "points_financial_invariants",
                "status": invariants.get("status"),
                "error_count": int(invariants.get("error_count") or 0),
                "model": invariants.get("model"),
            }

        sql_started = time.perf_counter()
        started = start_management_plane_job(
            get_db=get_db,
            actor=actor_snapshot,
            job_type="points_financial_invariants",
            title="Points financial invariants",
            snapshot_key="points_financial_invariants",
            request_payload={},
            worker=worker,
            summary_builder=summary,
            reuse_recent_success_seconds=30,
            queue_class="points_chain_admin",
            resource_locks=("finance_db",),
        )
        request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        return started

    @app.route("/api/root/points/financial-invariants", methods=["GET"])
    @require_csrf_safe
    def root_points_financial_invariants():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        refresh = str(request.args.get("refresh") or request.args.get("start_job") or "").strip().lower() in {"1", "true", "yes", "on"}
        if not refresh:
            conn = get_db()
            try:
                snapshot = get_management_snapshot(conn, snapshot_key="points_financial_invariants", include_payload=True)
            finally:
                conn.close()
            if snapshot.get("ok"):
                payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
                return json_resp({
                    "ok": bool(payload.get("ok", True)),
                    "financial_invariants": payload.get("financial_invariants") or {},
                    "snapshot": {
                        "snapshot_key": snapshot.get("snapshot_key"),
                        "generated_at": snapshot.get("generated_at"),
                        "source_job_uuid": snapshot.get("source_job_uuid"),
                        "summary": snapshot.get("summary") or {},
                        "snapshot_backed": True,
                    },
                })
        started = start_root_points_financial_invariants_job(actor)
        return management_job_response(started, snapshot_key="points_financial_invariants")

    @app.route("/api/root/points/financial-invariants/jobs", methods=["POST"])
    @require_csrf
    def root_points_financial_invariants_start_job():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        started = start_root_points_financial_invariants_job(actor)
        return management_job_response(started, snapshot_key="points_financial_invariants")

    @app.route("/api/root/points/financial-invariants/latest", methods=["GET"])
    @require_csrf_safe
    def root_points_financial_invariants_latest():
        actor, err = root_or_403()
        if err:
            return err
        return management_snapshot_response("points_financial_invariants", payload_key="financial_invariants")

    @app.route("/api/root/points/financial-invariants/jobs/<path:job_uuid>", methods=["GET"])
    @require_csrf_safe
    def root_points_financial_invariants_job(job_uuid):
        actor, err = root_or_403()
        if err:
            return err
        return management_job_status_response(job_uuid)

    @app.route("/api/root/points/audit", methods=["GET"])
    @require_csrf_safe
    def root_points_audit():
        actor, err = root_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        limit = parse_positive_int(request.args.get("limit"), default=100, maximum=200) or 100
        return json_resp({"ok": True, "audit_logs": points_service.list_chain_audit_logs(limit=limit)})

    @app.route("/api/root/points/ledger/<ledger_uuid>/rollback", methods=["POST"])
    @require_csrf
    def root_points_ledger_rollback(ledger_uuid):
        return json_resp({
            "ok": False,
            "code": "blockchain_permission_model",
            "msg": "私有鏈模式不允許 rollback 既有交易；修正必須以新的鏈上補償交易追加。",
        }, 410)

    def start_admin_points_economy_stats_job(actor):
        actor_snapshot = management_actor(actor)

        def worker(progress):
            progress(stage="bounded_verify", progress_percent=15, detail="building bounded verification for economy stats")
            verification = points_service.verify_chain_bounded_snapshot()
            progress(stage="economy_stats", progress_percent=45, detail="building economy stats off request path")
            stats = points_service.economy_stats(verification=verification)
            progress(stage="snapshot", progress_percent=90, detail="recording economy stats snapshot")
            return {"ok": True, "stats": stats, "verification": verification}

        def summary(payload):
            stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
            circulation = stats.get("circulation") if isinstance(stats.get("circulation"), dict) else {}
            verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
            return {
                "ok": bool(payload.get("ok", True)),
                "snapshot_key": "points_economy_stats",
                "verification_ok": bool(verification.get("ok")),
                "bounded_verify": bool(verification.get("bounded")),
                "wallet_count": int(circulation.get("wallet_count") or 0),
                "outstanding_points": int(circulation.get("outstanding_points") or 0),
                "unsealed_entries": int(circulation.get("unsealed_entries") or 0),
            }

        sql_started = time.perf_counter()
        started = start_management_plane_job(
            get_db=get_db,
            actor=actor_snapshot,
            job_type="points_economy_stats",
            title="Points economy stats",
            snapshot_key="points_economy_stats",
            request_payload={},
            worker=worker,
            summary_builder=summary,
            reuse_recent_success_seconds=15,
            queue_class="points_chain_admin",
            resource_locks=("finance_db",),
        )
        request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        return started

    def start_admin_points_operations_snapshot_job(actor):
        actor_snapshot = management_actor(actor)

        def worker(progress):
            progress(stage="operations_snapshot", progress_percent=30, detail="building bounded points operations snapshot")
            snapshot = points_service.operations_control_snapshot()
            progress(stage="snapshot", progress_percent=90, detail="recording operations snapshot")
            return {"ok": bool(snapshot.get("ok", True)), "operations": snapshot}

        def summary(payload):
            operations = payload.get("operations") if isinstance(payload.get("operations"), dict) else {}
            return {
                "ok": bool(operations.get("ok", payload.get("ok", True))),
                "snapshot_key": "points_operations_control",
                "warning_count": int(operations.get("warning_count") or 0),
                "chain_branch": operations.get("chain_branch"),
                "unsealed_entries": ((operations.get("private_chain") or {}).get("unsealed_entries") if isinstance(operations.get("private_chain"), dict) else 0),
                "pending_transfers": ((operations.get("exchange_operations") or {}).get("pending_transfers") if isinstance(operations.get("exchange_operations"), dict) else 0),
            }

        sql_started = time.perf_counter()
        started = start_management_plane_job(
            get_db=get_db,
            actor=actor_snapshot,
            job_type="points_operations_control",
            title="Points operations control snapshot",
            snapshot_key="points_operations_control",
            request_payload={},
            worker=worker,
            summary_builder=summary,
            reuse_recent_success_seconds=15,
            queue_class="points_chain_admin",
            resource_locks=("finance_db",),
        )
        request.environ["hackme.sql_ms"] = round((time.perf_counter() - sql_started) * 1000, 3)
        return started

    @app.route("/api/admin/points/economy/stats", methods=["GET"])
    @require_csrf_safe
    def admin_points_economy_stats():
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        refresh = str(request.args.get("refresh") or request.args.get("start_job") or "").strip().lower() in {"1", "true", "yes", "on"}
        if not refresh:
            conn = get_db()
            try:
                snapshot = get_management_snapshot(conn, snapshot_key="points_economy_stats", include_payload=True)
            finally:
                conn.close()
            if snapshot.get("ok"):
                payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
                return json_resp({
                    "ok": bool(payload.get("ok", True)),
                    "stats": payload.get("stats") or {},
                    "snapshot": {
                        "snapshot_key": snapshot.get("snapshot_key"),
                        "generated_at": snapshot.get("generated_at"),
                        "source_job_uuid": snapshot.get("source_job_uuid"),
                        "summary": snapshot.get("summary") or {},
                        "snapshot_backed": True,
                    },
                })
        started = start_admin_points_economy_stats_job(actor)
        return management_job_response(started, snapshot_key="points_economy_stats")

    @app.route("/api/admin/points/economy/stats/jobs", methods=["POST"])
    @require_csrf
    def admin_points_economy_stats_start_job():
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        started = start_admin_points_economy_stats_job(actor)
        return management_job_response(started, snapshot_key="points_economy_stats")

    @app.route("/api/admin/points/economy/stats/latest", methods=["GET"])
    @require_csrf_safe
    def admin_points_economy_stats_latest():
        actor, err = manager_or_403()
        if err:
            return err
        return management_snapshot_response("points_economy_stats", payload_key="stats")

    @app.route("/api/admin/points/economy/stats/jobs/<path:job_uuid>", methods=["GET"])
    @require_csrf_safe
    def admin_points_economy_stats_job(job_uuid):
        actor, err = manager_or_403()
        if err:
            return err
        return management_job_status_response(job_uuid)

    @app.route("/api/admin/points/operations/snapshot", methods=["GET"])
    @require_csrf_safe
    def admin_points_operations_snapshot():
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        refresh = str(request.args.get("refresh") or request.args.get("start_job") or "").strip().lower() in {"1", "true", "yes", "on"}
        if not refresh:
            conn = get_db()
            try:
                snapshot = get_management_snapshot(conn, snapshot_key="points_operations_control", include_payload=True)
            finally:
                conn.close()
            if snapshot.get("ok"):
                payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
                return json_resp({
                    "ok": bool(payload.get("ok", True)),
                    "operations": payload.get("operations") or {},
                    "snapshot": {
                        "snapshot_key": snapshot.get("snapshot_key"),
                        "generated_at": snapshot.get("generated_at"),
                        "source_job_uuid": snapshot.get("source_job_uuid"),
                        "summary": snapshot.get("summary") or {},
                        "snapshot_backed": True,
                    },
                })
        started = start_admin_points_operations_snapshot_job(actor)
        return management_job_response(started, snapshot_key="points_operations_control")

    @app.route("/api/admin/points/operations/snapshot/jobs", methods=["POST"])
    @require_csrf
    def admin_points_operations_snapshot_start_job():
        actor, err = manager_or_403()
        if err:
            return err
        if not points_chain_enabled():
            return points_chain_disabled_response()
        started = start_admin_points_operations_snapshot_job(actor)
        return management_job_response(started, snapshot_key="points_operations_control")

    @app.route("/api/admin/points/operations/snapshot/latest", methods=["GET"])
    @require_csrf_safe
    def admin_points_operations_snapshot_latest():
        actor, err = manager_or_403()
        if err:
            return err
        return management_snapshot_response("points_operations_control", payload_key="operations")

    @app.route("/api/admin/points/operations/snapshot/jobs/<path:job_uuid>", methods=["GET"])
    @require_csrf_safe
    def admin_points_operations_snapshot_job(job_uuid):
        actor, err = manager_or_403()
        if err:
            return err
        return management_job_status_response(job_uuid)
