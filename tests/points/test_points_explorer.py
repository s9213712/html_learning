import json
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps

import pytest
from flask import Flask, jsonify, make_response

from routes.economy import register_economy_routes
from services.points_chain import (
    DISPLAY_CURRENCY,
    PointsLedgerService,
    create_official_hot_wallet,
    ensure_points_economy_schema,
)
from services.points_chain.economy_layer import economy_fund_address


def _json_resp(payload, status=200):
    return make_response(jsonify(payload), status)


def _passthrough(fn):
    return fn


def _blocked_safe_get_guard(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        return _json_resp({"ok": False, "msg": "unexpected safe csrf guard"}, 418)

    return wrapper


def _blocked_post_csrf_guard(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        return _json_resp({"ok": False, "error": "csrf_invalid"}, 403)

    return wrapper


def _get_db_factory(path):
    def get_db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    return get_db


def _build_app(tmp_path, *, require_csrf=None, require_csrf_safe=None, settings=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "explorer.db"
    get_db = _get_db_factory(db_path)
    conn = get_db()
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active')"
    )
    conn.execute("INSERT INTO users (id, username, role, status) VALUES (1, 'root', 'super_admin', 'active')")
    conn.execute("INSERT INTO users (id, username, role, status) VALUES (2, 'test', 'user', 'active')")
    conn.execute("INSERT INTO users (id, username, role, status) VALUES (3, 'recipient', 'user', 'active')")
    conn.execute("INSERT INTO users (id, username, role, status) VALUES (4, 'manager', 'manager', 'active')")
    ensure_points_economy_schema(conn)
    conn.commit()
    conn.close()

    points = PointsLedgerService(get_db=get_db, chain_secret="test-secret", backup_dir=tmp_path / "backups")
    auto_ledger = points.record_transaction(
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="credit",
        amount=100,
        action_type="user_initial_grant",
        reference_type="test",
        reference_id="initial",
        idempotency_key="explorer:test:initial",
        reason="test initial points",
        actor={"id": 1, "username": "root", "role": "super_admin"},
    )["ledger"]
    manual_ledger = points.record_transaction(
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="credit",
        amount=20,
        action_type="admin_adjust_credit",
        reference_type="admin_adjustment",
        reference_id="manual-root-credit",
        idempotency_key="explorer:test:manual-root-credit",
        reason="root manual official wallet credit",
        actor={"id": 1, "username": "root", "role": "super_admin"},
    )["ledger"]

    current = {"id": 2, "username": "test", "role": "user"}
    app = Flask(__name__)
    app.testing = True
    register_economy_routes(app, {
        "get_current_user_ctx": lambda: current,
        "json_resp": _json_resp,
        "require_csrf": require_csrf or _passthrough,
        "require_csrf_safe": require_csrf_safe or _passthrough,
        "points_service": points,
        "role_rank": lambda role: {"user": 0, "manager": 1, "super_admin": 2}.get(role or "user", 0),
        "audit": lambda *args, **kwargs: None,
        "get_db": get_db,
        "get_system_settings": lambda: dict(settings or {}),
    })
    return app, points, auto_ledger, manual_ledger, current


def test_points_explorer_get_routes_are_public_safe_gets(tmp_path):
    app, points, ledger, _manual_ledger, current = _build_app(
        tmp_path,
        require_csrf_safe=_blocked_safe_get_guard,
    )
    current.clear()
    client = app.test_client()

    wallet_address = points.get_wallet(2)["active_wallet_address"]

    search_res = client.get(f"/api/points/explorer/search?q={ledger['ledger_uuid']}")
    tx_res = client.get(f"/api/points/explorer/tx/{ledger['ledger_uuid']}")
    wallet_res = client.get(f"/api/points/explorer/wallet/{wallet_address}")
    block_res = client.get("/api/points/explorer/block/999999")
    fee_res = client.get("/api/points/explorer/fee-estimate?fee_points=20")

    assert search_res.status_code == 200
    assert tx_res.status_code == 200
    assert wallet_res.status_code == 200
    assert block_res.status_code == 404
    assert fee_res.status_code == 200


def test_root_management_snapshot_missing_ok_keeps_console_clean_without_changing_default_404(tmp_path):
    app, _points, _ledger, _manual_ledger, current = _build_app(tmp_path)
    current.update({"id": 1, "username": "root", "role": "super_admin"})
    client = app.test_client()

    default_res = client.get("/api/root/management/snapshots/points_root_report")
    assert default_res.status_code == 404
    default_body = default_res.get_json()
    assert default_body["ok"] is False
    assert default_body["snapshot"]["missing"] is True

    missing_ok_res = client.get("/api/root/management/snapshots/points_root_report?missing_ok=1")
    assert missing_ok_res.status_code == 200
    missing_ok_body = missing_ok_res.get_json()
    assert missing_ok_body["ok"] is False
    assert missing_ok_body["snapshot"]["missing"] is True
    assert missing_ok_body["client_should_enqueue"] is True


def test_points_explorer_bridge_route_links_pc1_and_pc0_layers(tmp_path):
    app, points, _ledger, _manual_ledger, _current = _build_app(tmp_path)
    client = app.test_client()
    wallet = points.get_wallet(2)
    deposit = points.confirm_deposit_to_hot_wallet(
        actor={"id": 1, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("a" * 48),
        destination_address=wallet["deposit_address"],
        amount_points=33,
        chain_tx_hash="bridge-explorer-chain-tx",
        confirmations=20,
        required_confirmations=20,
        risk_status="accepted",
    )
    bridge_uuid = deposit["bridge_event"]["bridge_uuid"]

    res = client.get(f"/api/points/explorer/bridge/{bridge_uuid}")
    by_chain_hash = client.get("/api/points/explorer/bridge/bridge-explorer-chain-tx")
    search_res = client.get(f"/api/points/explorer/search?q={bridge_uuid}")

    assert res.status_code == 200
    payload = res.get_json()["result"]
    assert payload["kind"] == "bridge"
    assert payload["layer"] == "bridge"
    bridge = payload["bridge_event"]
    assert bridge["asset_type"] == "Cross-Ledger Settlement Event"
    assert bridge["pc1_settlement_tx"] == "bridge-explorer-chain-tx"
    assert bridge["pc0_wrapped_credit"] == deposit["ledger"]["ledger_uuid"]
    assert bridge["pc0_hot_wallet"] == wallet["active_wallet_address"]
    assert bridge["invariant_status"] == "valid"
    assert bridge["internal_transaction"]["layer"] == "pc0"
    assert by_chain_hash.status_code == 200
    assert search_res.status_code == 200
    assert search_res.get_json()["result"]["kind"] == "bridge"


def test_points_explorer_shows_mint_genesis_fund_events(tmp_path):
    app, points, _ledger, _manual_ledger, _current = _build_app(tmp_path)
    client = app.test_client()
    mint_address = economy_fund_address(points.chain_secret, "mint")

    wallet_res = client.get(f"/api/points/explorer/wallet/{mint_address}")
    block_res = client.get("/api/points/explorer/block/0")

    assert wallet_res.status_code == 200
    wallet = wallet_res.get_json()["result"]["wallet"]
    assert wallet["fund_key"] == "mint"
    assert wallet["points_balance"] > 0
    assert wallet["transaction_count"] >= 3
    txs = wallet["recent_transactions"]
    destinations = {tx["wallet_flow"]["destination_fund_key"] for tx in txs}
    assert {"official_treasury", "promo_fund", "exchange_fund"}.issubset(destinations)
    assert {tx["wallet_flow"]["source_fund_key"] for tx in txs} == {"mint"}

    assert block_res.status_code == 200
    block = block_res.get_json()["result"]["block"]
    assert block["block_number"] == 0
    assert block["seal_status"] == "virtual_economy_genesis"
    assert block["transaction_count"] >= 3
    assert {tx["wallet_flow"]["source_fund_key"] for tx in block["transactions"]} == {"mint"}


def test_points_chain_can_be_disabled_without_disabling_basic_points(tmp_path):
    app, _points, _ledger, _manual_ledger, current = _build_app(
        tmp_path,
        settings={"feature_economy_enabled": True, "feature_points_chain_enabled": False},
    )
    client = app.test_client()

    wallet_res = client.get("/api/points/wallet")
    ledger_res = client.get("/api/points/ledger?limit=10")
    catalog_res = client.get("/api/points/catalog")
    spend_res = client.post(
        "/api/points/spend",
        json={"item_key": "post_cost_standard", "quantity": 1, "request_uuid": "basic-mode-spend-1"},
    )

    assert wallet_res.status_code == 200
    assert ledger_res.status_code == 200
    assert catalog_res.status_code == 200
    assert spend_res.status_code == 200
    assert spend_res.get_json()["ledger"]["direction"] == "debit"
    assert spend_res.get_json()["settlement_policy"] == "basic_points_immediate_debit"

    for method, path in (
        ("get", "/api/points/wallet/onboarding"),
        ("get", "/api/points/transactions?limit=10"),
        ("get", "/api/points/explorer/fee-estimate?fee_points=1"),
        ("get", f"/api/points/ledger/{_ledger['ledger_uuid']}/proof"),
        ("post", "/api/points/transactions/submit"),
    ):
        response = getattr(client, method)(path, json={}) if method == "post" else getattr(client, method)(path)
        assert response.status_code == 503
        assert response.get_json()["code"] == "points_chain_disabled"

    current.update({"id": 1, "username": "root", "role": "super_admin"})
    root_report = client.get("/api/root/points/report")
    finality_sweep = client.post("/api/root/points/finality-sweep", json={"limit": 5})
    root_grant = client.post(
        "/api/root/points/official-wallet/grant",
        json={"destination_wallet_address": "pc1deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "amount": 1},
    )
    assert root_report.status_code == 503
    assert root_report.get_json()["code"] == "points_chain_disabled"
    assert finality_sweep.status_code == 503
    assert finality_sweep.get_json()["code"] == "points_chain_disabled"
    assert root_grant.status_code == 503
    assert root_grant.get_json()["code"] == "points_chain_disabled"


def test_root_points_management_endpoints_start_async_jobs(tmp_path):
    app, _points, _ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    current.update({"id": 1, "username": "root", "role": "super_admin"})

    seal = client.post("/api/root/points/chain/seal", json={"limit": 5})
    verify = client.get("/api/root/points/chain/verify")
    report = client.get("/api/root/points/report?refresh=1")
    finality = client.post("/api/root/points/finality-sweep", json={"limit": 5})
    financial = client.get("/api/root/points/financial-invariants")
    recovery = client.post(
        "/api/root/points/chain/recovery/auto-handle",
        json={"confirm": "AUTO HANDLE POINTSCHAIN"},
    )

    for response in (seal, verify, report, finality, financial, recovery):
        payload = response.get_json()
        assert response.status_code == 202
        assert payload["ok"] is True
        assert payload["async"] is True
        assert payload["job_id"]
        status = client.get(payload["status_url"])
        assert status.status_code == 200
        assert status.get_json()["job"]["source_module"] == "management_plane"

    latest = client.get(seal.get_json()["latest_snapshot_url"])
    assert latest.status_code in {200, 404}
    finality_latest = client.get("/api/root/points/finality-sweep/latest")
    assert finality_latest.status_code in {200, 404}

    current.update({"id": 4, "username": "manager", "role": "manager"})
    stats = client.get("/api/admin/points/economy/stats")
    operations = client.get("/api/admin/points/operations/snapshot")
    for response in (stats, operations):
        payload = response.get_json()
        assert response.status_code == 202
        assert payload["ok"] is True
        assert payload["async"] is True
        assert payload["job_id"]
        assert payload["queue_class"] == "points_chain_admin"
        assert payload["resource_locks"] == ["finance_db"]


def test_wallet_transaction_submit_compact_response(tmp_path):
    app, points, _ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        sender_wallet = create_official_hot_wallet(conn, user_id=2, chain_secret="test-secret", label="sender hot")
        recipient_wallet = create_official_hot_wallet(conn, user_id=3, chain_secret="test-secret", label="recipient hot")
        conn.commit()
    finally:
        conn.close()
    points.record_transaction(
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="credit",
        amount=50,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="compact-submit-source",
        idempotency_key="explorer:test:compact-submit-source",
        reason="compact submit source",
        actor={"id": 1, "username": "root", "role": "super_admin"},
    )

    current.update({"id": 2, "username": "test", "role": "user"})
    res = client.post(
        "/api/points/transactions/submit",
        json={
            "source_wallet_address": sender_wallet["address"],
            "destination_wallet_address": recipient_wallet["address"],
            "amount_points": 5,
            "fee_points": 1,
            "request_uuid": "compact-submit-transfer",
            "memo": "compact submit",
            "compact": True,
        },
    )
    payload = res.get_json()
    assert res.status_code == 200
    assert payload["compact"] is True
    assert payload["transaction_hash"]
    assert payload["request_uuid"] == "compact-submit-transfer"
    assert "transaction" not in payload


def test_points_explorer_searches_transaction_and_wallet_with_finality_rule(tmp_path):
    app, points, ledger, _manual_ledger, _current = _build_app(tmp_path)
    client = app.test_client()

    tx_res = client.get(f"/api/points/explorer/search?q={ledger['ledger_uuid']}")
    tx_payload = tx_res.get_json()

    assert tx_res.status_code == 200
    assert tx_payload["result"]["kind"] == "transaction"
    tx = tx_payload["result"]["transaction"]
    assert tx["ledger_uuid"] == ledger["ledger_uuid"]
    assert tx["finality"]["target_proved_count"] == 0
    assert tx["finality"]["fee_model"] == "internal_ledger_no_priority_fee_v1"
    assert tx["finality"]["network_fee_state"]["congestion_label"] == "internal"
    assert tx["finality"]["human_rule"] == "pc0 Inner Address 使用站內帳本即時成交；免 20 Proved、免 priority fee"
    assert tx["finality"]["chain_fee_policy"]["base_fee_exempt"] is True
    assert tx["finality"]["chain_fee_policy"]["acceleration_allowed"] is False
    assert tx["finality"]["chain_fee_policy"]["exemption_reason"] == "pc0 Inner Address 使用站內帳本即時成交；免 20 Proved、免 priority fee"
    assert "wallet_flow_snapshot" not in tx["input_data"]
    assert tx["wallet_flow"]["destination_wallet_address"]

    wallet_address = points.get_wallet(2)["active_wallet_address"]
    wallet_res = client.get(f"/api/points/explorer/search?q={wallet_address}")
    wallet_payload = wallet_res.get_json()

    assert wallet_res.status_code == 200
    assert wallet_payload["result"]["kind"] == "wallet"
    wallet = wallet_payload["result"]["wallet"]
    assert wallet["address"] == wallet_address
    assert wallet["legacy_account"] is False
    assert wallet["address_type"] == "inner_address"
    assert wallet["points_balance"] == 120
    assert wallet["transaction_count"] == 2
    assert wallet["human_rule"] == "pc0 Inner Address 使用站內帳本即時成交；免 20 Proved、免 priority fee"
    assert wallet["finality_rule"]["target_proved_count"] == 0


def test_points_explorer_acceleration_is_append_only_and_idempotent(tmp_path):
    app, points, _auto_ledger, _manual_ledger, _current = _build_app(tmp_path)
    client = app.test_client()
    source_wallet = points.get_wallet(2)["active_wallet_address"]
    ledger = points.record_transaction(
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="transfer_out",
        amount=1,
        action_type="wallet_transfer_out",
        reference_type="test",
        reference_id="cold-withdrawal-ledger",
        idempotency_key="explorer:test:cold-withdrawal-ledger",
        reason="cold withdrawal explorer acceleration",
        public_metadata={
            "source_wallet_address": source_wallet,
            "destination_wallet_address": "pc1" + "9" * 48,
            "settlement_rail": "withdrawal_bridge_lock",
            "chain_required": True,
            "approval_required": True,
            "network_fee_points": 1,
            "service_fee_points": 0,
        },
        actor={"id": 2, "username": "test", "role": "user"},
    )["ledger"]

    first = client.post(
        "/api/points/explorer/accelerate",
        json={"ledger_ref": ledger["ledger_uuid"], "fee_points": 10, "request_uuid": "accel-test-1"},
    )
    first_payload = first.get_json()

    assert first.status_code == 200
    assert first_payload["created"] is True
    assert first_payload["acceleration"]["fee_points"] == 10
    assert first_payload["result"]["transaction"]["finality"]["acceleration_fee_paid_points"] == 10
    assert first_payload["result"]["transaction"]["finality"]["acceleration_fee_destination_fund_key"] == "burn"
    assert first_payload["result"]["transaction"]["finality"]["transaction_fee_points"] == 10
    assert first_payload["result"]["transaction"]["finality"]["gas_price_points_per_proved"] == 0.5
    assert first_payload["result"]["transaction"]["finality"]["chain_fee_policy"]["acceleration_allowed"] is True
    assert first_payload["fee_ledger"]["action_type"] == "chain_acceleration_fee"

    second = client.post(
        "/api/points/explorer/accelerate",
        json={"ledger_ref": ledger["ledger_uuid"], "fee_points": 10, "request_uuid": "accel-test-1"},
    )
    second_payload = second.get_json()

    assert second.status_code == 200
    assert second_payload["created"] is False
    assert points.get_wallet(2)["points_balance"] == 109

    conflict = client.post(
        "/api/points/explorer/accelerate",
        json={"ledger_ref": ledger["ledger_uuid"], "fee_points": 11, "request_uuid": "accel-test-1"},
    )
    assert conflict.status_code == 400
    assert "idempotency key conflict" in conflict.get_json()["msg"]
    assert points.get_wallet(2)["points_balance"] == 109

    wallet_address = points.get_wallet(2)["active_wallet_address"]
    wallet_res = client.get(f"/api/points/explorer/wallet/{wallet_address}")
    wallet = wallet_res.get_json()["result"]["wallet"]
    assert wallet["points_balance"] == 109
    assert wallet["transaction_count"] == 4


def test_points_explorer_fee_estimate_reflects_network_congestion(tmp_path):
    app, points, _auto_ledger, _manual_ledger, _current = _build_app(tmp_path)
    client = app.test_client()

    idle = client.get("/api/points/explorer/fee-estimate?fee_points=1").get_json()["estimate"]
    source = points.get_wallet(2)["public_account_id"]
    destination = points.get_wallet(3)["public_account_id"]
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        for index in range(50):
            conn.execute(
                """
                INSERT INTO points_chain_transfer_requests (
                    request_uuid, request_hash, tx_group_hash, sender_user_id, recipient_user_id,
                    source_wallet_address, destination_wallet_address, amount_points, fee_points, memo, status, created_at
                ) VALUES (?, ?, ?, 2, 3, ?, ?, 1, 1, '', 'pending', ?)
                """,
                (
                    f"busy-fee-{index}",
                    f"busy-request-hash-{index}",
                    f"busy-tx-hash-{index}",
                    source,
                    destination,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    busy = client.get("/api/points/explorer/fee-estimate?fee_points=1").get_json()["estimate"]
    accelerated = client.get("/api/points/explorer/fee-estimate?fee_points=200").get_json()["estimate"]

    assert idle["network_fee_state"]["congestion_label"] == "idle"
    assert busy["network_fee_state"]["congestion_label"] == "congested"
    assert busy["network_fee_state"]["suggested_total_fee_points"] > idle["network_fee_state"]["suggested_total_fee_points"]
    assert busy["estimated_seconds_min"] > idle["estimated_seconds_min"]
    assert accelerated["estimated_seconds_max"] < busy["estimated_seconds_max"]


def test_points_explorer_auto_distribution_is_chain_fee_exempt(tmp_path):
    app, points, ledger, _manual_ledger, _current = _build_app(tmp_path)
    client = app.test_client()

    res = client.post(
        "/api/points/explorer/accelerate",
        json={"ledger_ref": ledger["ledger_uuid"], "fee_points": 10, "request_uuid": "auto-exempt-1"},
    )

    assert res.status_code == 400
    assert "pc0 Inner Address 使用站內帳本即時成交" in res.get_json()["msg"]
    assert points.get_wallet(2)["points_balance"] == 120


def test_points_explorer_pending_proved_count_uses_stable_realistic_schedule(tmp_path):
    _app, points, _auto_ledger, _manual_ledger, _current = _build_app(tmp_path)
    source_wallet = points.get_wallet(2)["active_wallet_address"]
    ledger = points.record_transaction(
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="transfer_out",
        amount=1,
        action_type="wallet_transfer_out",
        reference_type="test",
        reference_id="stable-schedule-cold-ledger",
        idempotency_key="explorer:test:stable-schedule-cold-ledger",
        reason="cold withdrawal explorer schedule",
        public_metadata={
            "source_wallet_address": source_wallet,
            "destination_wallet_address": "pc1" + "8" * 48,
            "settlement_rail": "withdrawal_bridge_lock",
            "chain_required": True,
            "approval_required": True,
            "network_fee_points": 1,
            "service_fee_points": 0,
        },
        actor={"id": 2, "username": "test", "role": "user"},
    )["ledger"]
    created_at = (datetime.now(timezone.utc) - timedelta(seconds=20)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    fake_ledger = {**dict(ledger), "created_at": created_at}
    conn = points.get_db()
    try:
        first = points._explorer_finality_for_ledger(conn, fake_ledger)
        second = points._explorer_finality_for_ledger(conn, fake_ledger)
    finally:
        conn.close()

    assert first["finality_simulation"] == "deterministic_proved_schedule_v1"
    assert first["settlement_seconds"] >= first["estimated_seconds_min"]
    assert first["settlement_seconds"] <= first["estimated_seconds_max"]
    assert 1 <= first["proved_count"] < 20
    assert first["proved_count"] == second["proved_count"]
    assert first["next_proof_eta_seconds"] >= 0
    assert first["eta_seconds"] <= first["settlement_seconds"]


def test_wallet_transfer_pending_does_not_credit_chain_address_until_proved(tmp_path):
    app, points, _auto_ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        sender_wallet = create_official_hot_wallet(conn, user_id=2, chain_secret="test-secret", label="sender hot")
        conn.commit()
    finally:
        conn.close()

    points.record_transaction(
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="credit",
        amount=200,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="transfer-funding",
        idempotency_key="explorer:test:transfer-funding",
        reason="fund transfer source",
        actor={"id": 1, "username": "root", "role": "super_admin"},
    )
    before_sender = points.explorer_wallet(sender_wallet["address"])["wallet"]["points_balance"]
    destination_address = "pc1" + ("8" * 48)

    current.update({"id": 2, "username": "test", "role": "user"})
    res = client.post(
        "/api/points/transactions/submit",
        json={
            "source_wallet_address": sender_wallet["address"],
            "destination_wallet_address": destination_address,
            "amount_points": 30,
            "fee_points": 1,
            "request_uuid": "pending-transfer-1",
            "memo": "acceptance transfer",
        },
    )
    payload = res.get_json()

    assert res.status_code == 200
    assert payload["created"] is True
    tx_hash = payload["tx_group_hash"]
    assert payload["transaction_hash"] == tx_hash
    assert payload["warnings"] == []
    assert payload["notifications"]["pending"]["all_sent"] is True
    assert payload["transaction"]["status"] == "pending"
    assert payload["transaction"]["settlement_rail"] == "withdrawal_bridge_lock"
    assert payload["transaction"]["wallet_flow"]["destination_unowned"] is True
    assert payload["transaction"]["finality"]["proved_count"] < 20
    assert payload["transaction"]["finality"]["chain_fee_policy"]["base_fee_destination_fund_key"] == "burn"
    assert payload["transaction"]["finality"]["chain_fee_policy"]["base_fee_destination_label"] == "BURN 銷毀錢包"
    assert payload["transaction"]["finality"]["chain_fee_policy"]["acceleration_allowed"] is True
    base_eta = payload["transaction"]["finality"]["settlement_seconds"]

    accel = client.post(
        "/api/points/explorer/accelerate",
        json={"ledger_ref": tx_hash, "fee_points": 20, "request_uuid": "pending-transfer-accel-1"},
    )
    accel_payload = accel.get_json()
    assert accel.status_code == 200, accel_payload
    assert accel_payload["created"] is True
    assert accel_payload["result"]["transaction"]["transaction_hash"] == tx_hash
    assert accel_payload["result"]["transaction"]["status"] == "pending"
    assert accel_payload["result"]["transaction"]["finality"]["acceleration_fee_paid_points"] == 20
    assert accel_payload["result"]["transaction"]["finality"]["acceleration_fee_destination_fund_key"] == "burn"
    assert accel_payload["result"]["transaction"]["finality"]["settlement_seconds"] < base_eta
    assert accel_payload["fee_ledger"]["action_type"] == "chain_acceleration_fee"

    accel_repeat = client.post(
        "/api/points/explorer/accelerate",
        json={"ledger_ref": tx_hash, "fee_points": 20, "request_uuid": "pending-transfer-accel-1"},
    )
    assert accel_repeat.status_code == 200
    assert accel_repeat.get_json()["created"] is False

    sender_transactions = client.get("/api/points/transactions?limit=10").get_json()
    assert sender_transactions["summary"]["pending_count"] == 1
    assert sender_transactions["summary"]["pending_outgoing_points"] == 31
    assert sender_transactions["transactions"][0]["transaction_hash"] == tx_hash
    assert sender_transactions["transactions"][0]["direction"] == "outgoing"
    assert sender_transactions["transactions"][0]["status"] == "pending"
    assert sender_transactions["transactions"][0]["balance_effect"] == "pending_no_recipient_credit"

    sender_pending = points.explorer_wallet(sender_wallet["address"])["wallet"]
    recipient_pending = points.explorer_wallet(destination_address)["wallet"]
    assert sender_pending["points_balance"] == before_sender - 31 - 20
    assert sender_pending["points_frozen"] == 31
    assert sender_pending["pending_outgoing_points"] == 31
    assert recipient_pending["points_balance"] == 0
    assert recipient_pending["points_frozen"] == 0

    sender_wallet_payload = points.get_wallet(2)
    assert sender_wallet_payload["wallet_identity_balances"][sender_wallet["address"]]["points_balance"] == before_sender - 31 - 20
    assert sender_wallet_payload["wallet_identity_balances"][sender_wallet["address"]]["points_frozen"] == 31
    assert sender_wallet_payload["wallet_identity_balances"][sender_wallet["address"]]["pending_outgoing_points"] == 31
    points.upsert_catalog_item(
        actor={"id": 1, "username": "root", "role": "super_admin"},
        item_key="pending_reserved_spend",
        item_name="Pending Reserved Spend",
        category="test",
        base_price=sender_pending["points_balance"] + 1,
        enabled=True,
    )
    with pytest.raises(ValueError, match="insufficient balance"):
        points.spend_points(
            user_id=2,
            item_key="pending_reserved_spend",
            source_wallet_address=sender_wallet["address"],
            actor={"id": 2, "username": "test", "role": "user"},
        )

    conn = points.get_db()
    try:
        rows = conn.execute(
            "SELECT user_id, type, body FROM notifications WHERE type='points_chain_transfer_pending' ORDER BY user_id"
        ).fetchall()
    finally:
        conn.close()
    assert [int(row["user_id"]) for row in rows] == [2]
    assert all(tx_hash in row["body"] for row in rows)

    proved_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_transfer_requests SET created_at=? WHERE tx_group_hash=?",
            (proved_at, tx_hash),
        )
        conn.commit()
    finally:
        conn.close()

    proved_res = client.get(f"/api/points/explorer/search?q={tx_hash}")
    assert proved_res.status_code == 200, proved_res.get_json()
    proved = proved_res.get_json()["result"]["transaction"]
    assert proved["status"] == "confirmed"
    assert proved["finality"]["finality_status"] == "proved"
    assert proved["finality"]["proved_count"] == 20
    sender_confirmed_transactions = client.get("/api/points/transactions?limit=10").get_json()
    assert sender_confirmed_transactions["summary"]["confirmed_count"] == 1
    confirmed_tx = sender_confirmed_transactions["transactions"][0]
    assert confirmed_tx["status"] == "confirmed"
    assert confirmed_tx["balance_effect"] == "confirmed"
    assert confirmed_tx["settlement_rail"] == "withdrawal_bridge_lock"
    out_ledger_uuid = confirmed_tx["transfer_ledgers"]["transfer_out_ledger_uuid"]
    fee_ledger_uuid = confirmed_tx["transfer_ledgers"]["fee_ledger_uuid"]
    assert out_ledger_uuid
    assert fee_ledger_uuid

    out_ledger_res = client.get(f"/api/points/explorer/search?q={out_ledger_uuid}")
    assert out_ledger_res.status_code == 200, out_ledger_res.get_json()
    out_ledger = out_ledger_res.get_json()["result"]["transaction"]
    assert out_ledger["settlement_rail"] == "withdrawal_bridge_lock"
    assert out_ledger["wallet_flow"]["settlement_rail"] == "withdrawal_bridge_lock"
    assert out_ledger["input_data"]["chain_required"] is True
    assert out_ledger["input_data"]["network_fee_points"] == 1

    fee_ledger_res = client.get(f"/api/points/explorer/search?q={fee_ledger_uuid}")
    assert fee_ledger_res.status_code == 200, fee_ledger_res.get_json()
    fee_ledger = fee_ledger_res.get_json()["result"]["transaction"]
    assert fee_ledger["settlement_rail"] == "withdrawal_bridge_lock"
    assert fee_ledger["wallet_flow"]["network_fee_points"] == 1

    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        metadata_rows = conn.execute(
            "SELECT ledger_uuid, public_metadata_json FROM points_ledger WHERE ledger_uuid IN (?, ?)",
            (out_ledger_uuid, fee_ledger_uuid),
        ).fetchall()
        metadata_by_uuid = {row["ledger_uuid"]: json.loads(row["public_metadata_json"]) for row in metadata_rows}
        legacy_metadata = {
            key: value
            for key, value in metadata_by_uuid[out_ledger_uuid].items()
            if key not in {"pending_request_uuid", "request_uuid", "tx_group_hash", "settlement_rail", "chain_required", "approval_required", "network_fee_points", "service_fee_points"}
        }
        legacy_row = dict(conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (out_ledger_uuid,)).fetchone())
        legacy_row["public_metadata_json"] = json.dumps(legacy_metadata, sort_keys=True)
        legacy_payload = points._explorer_public_ledger(conn, legacy_row)
    finally:
        conn.close()
    for public_metadata in metadata_by_uuid.values():
        assert public_metadata["settlement_rail"] == "withdrawal_bridge_lock"
        assert public_metadata["chain_required"] is True
        assert public_metadata["approval_required"] is True
        assert public_metadata["network_fee_points"] == 1
        assert public_metadata["service_fee_points"] == 0

    assert legacy_payload["settlement_rail"] == "withdrawal_bridge_lock"

    sender_final = points.explorer_wallet(sender_wallet["address"])["wallet"]
    recipient_final = points.explorer_wallet(destination_address)["wallet"]
    assert sender_final["points_balance"] == before_sender - 31 - 20
    assert sender_final["points_frozen"] == 0
    assert sender_final["pending_outgoing_points"] == 0
    assert sender_final["transaction_count"] >= 4
    assert sender_final["received_tx_count"] >= 1
    assert sender_final["sent_tx_count"] >= 3
    assert sender_final["total_sent_points"] == 51
    assert [row["direction"] for row in sender_final["recent_transactions"][:3]] == ["debit", "transfer_out", "debit"]
    assert recipient_final["points_balance"] == 30
    assert recipient_final["transaction_count"] == 1
    assert recipient_final["received_tx_count"] == 1
    assert recipient_final["sent_tx_count"] == 0
    assert [row["direction"] for row in recipient_final["recent_transactions"]] == ["transfer_out"]

    conn = points.get_db()
    try:
        fee_events = conn.execute(
            """
            SELECT transaction_type, destination_fund_key, amount
            FROM points_economy_events
            WHERE transaction_type IN ('chain_acceleration_fee', 'wallet_transfer_fee')
            ORDER BY id
            """
        ).fetchall()
        rows = conn.execute(
            "SELECT user_id, type, body FROM notifications WHERE type='points_chain_transfer_completed' ORDER BY user_id"
        ).fetchall()
    finally:
        conn.close()
    assert [(row["transaction_type"], row["destination_fund_key"], int(row["amount"])) for row in fee_events] == [
        ("chain_acceleration_fee", "burn", 20),
        ("wallet_transfer_fee", "burn", 1),
    ]
    assert [int(row["user_id"]) for row in rows] == [2]
    assert all(tx_hash in row["body"] for row in rows)


def test_wallet_transfer_api_rejects_pc0_direct_chain_destination_with_guidance(tmp_path):
    app, points, _auto_ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        sender_wallet = create_official_hot_wallet(conn, user_id=2, chain_secret="test-secret", label="sender hot")
        conn.commit()
    finally:
        conn.close()

    current.update({"id": 2, "username": "test", "role": "user"})
    res = client.post(
        "/api/points/transactions/submit",
        json={
            "source_wallet_address": "pc1" + ("a" * 48),
            "destination_wallet_address": "pc0" + ("b" * 48),
            "amount_points": 10,
            "fee_points": 1,
            "request_uuid": "api-direct-chain-to-pc0-must-fail",
            "memo": "must use bridge",
        },
    )
    payload = res.get_json()

    assert res.status_code == 400
    assert payload["code"] == "pc0_internal_address_not_chain_reachable"
    assert "平台入金地址" in payload["msg"]
    assert "credit 到 pc0 站內託管錢包" in payload["guidance"]["deposit"]
    conn = points.get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM points_chain_transfer_requests WHERE request_uuid='api-direct-chain-to-pc0-must-fail'"
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_root_transaction_management_confirms_to_address_even_if_wallet_becomes_inactive(tmp_path):
    app, points, _auto_ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        sender_wallet = create_official_hot_wallet(conn, user_id=2, chain_secret="test-secret", label="sender hot")
        recipient_address = "pc1" + ("9" * 48)
        conn.commit()
    finally:
        conn.close()

    points.record_transaction(
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="credit",
        amount=50,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="inactive-wallet-source",
        idempotency_key="explorer:test:inactive-wallet-source",
        reason="fund transfer source",
        actor={"id": 1, "username": "root", "role": "super_admin"},
    )

    current.update({"id": 2, "username": "test", "role": "user"})
    res = client.post(
        "/api/points/transactions/submit",
        json={
            "source_wallet_address": sender_wallet["address"],
            "destination_wallet_address": recipient_address,
            "amount_points": 10,
            "fee_points": 1,
            "request_uuid": "inactive-wallet-transfer",
            "memo": "inactive wallet finality",
        },
    )
    assert res.status_code == 200, res.get_json()
    tx_hash = res.get_json()["transaction_hash"]

    proved_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_transfer_requests SET created_at=? WHERE tx_group_hash=?",
            (proved_at, tx_hash),
        )
        conn.commit()
    finally:
        conn.close()

    current.update({"id": 1, "username": "root", "role": "super_admin"})
    root_res = client.get("/api/points/transactions?limit=10&sweep=1")
    assert root_res.status_code == 200, root_res.get_json()
    payload = root_res.get_json()
    assert payload["summary"]["confirmed_count"] == 1
    assert payload["transactions"][0]["transaction_hash"] == tx_hash
    assert payload["transactions"][0]["status"] == "confirmed"
    assert payload["transactions"][0]["balance_effect"] == "confirmed"
    assert payload["transactions"][0]["wallet_flow"]["destination_unowned"] is True
    assert points.explorer_wallet(recipient_address)["wallet"]["points_balance"] == 10


def test_root_transaction_management_sweeps_proved_pending_beyond_page_limit(tmp_path):
    app, points, _auto_ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        sender_wallet = create_official_hot_wallet(conn, user_id=2, chain_secret="test-secret", label="sender hot")
        recipient_address = "pc1" + ("7" * 48)
        conn.commit()
    finally:
        conn.close()

    points.record_transaction(
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="credit",
        amount=500,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="batch-sweep-source",
        idempotency_key="explorer:test:batch-sweep-source",
        reason="fund transfer source",
        actor={"id": 1, "username": "root", "role": "super_admin"},
    )

    current.update({"id": 2, "username": "test", "role": "user"})
    for index in range(15):
        res = client.post(
            "/api/points/transactions/submit",
            json={
                "source_wallet_address": sender_wallet["address"],
                    "destination_wallet_address": recipient_address,
                "amount_points": 10,
                "fee_points": 1,
                "request_uuid": f"batch-sweep-transfer-{index}",
                "memo": "batch sweep finality",
            },
        )
        assert res.status_code == 200, res.get_json()

    proved_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_transfer_requests SET created_at=? WHERE request_uuid LIKE 'batch-sweep-transfer-%'",
            (proved_at,),
        )
        conn.commit()
    finally:
        conn.close()

    current.update({"id": 1, "username": "root", "role": "super_admin"})
    root_read = client.get("/api/points/transactions?limit=5")
    assert root_read.status_code == 200, root_read.get_json()
    read_payload = root_read.get_json()
    assert read_payload["summary"]["maintenance_run"] is False
    assert read_payload["summary"]["batch_checked_count"] == 0
    assert read_payload["summary"]["pending_count"] == 5
    assert all(item["status"] == "pending" for item in read_payload["transactions"])

    root_res = client.get("/api/points/transactions?limit=5&sweep=1")
    assert root_res.status_code == 200, root_res.get_json()
    payload = root_res.get_json()
    assert payload["summary"]["maintenance_run"] is True
    assert payload["summary"]["batch_checked_count"] == 5
    assert payload["summary"]["batch_finalized_count"] == 5
    assert payload["summary"]["batch_confirmed_count"] == 5
    assert payload["summary"]["pending_count"] == 0
    assert all(item["status"] == "confirmed" for item in payload["transactions"])

    conn = points.get_db()
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM points_chain_transfer_requests WHERE request_uuid LIKE 'batch-sweep-transfer-%' AND status='pending'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert remaining == 5

    for _ in range(2):
        root_res = client.get("/api/points/transactions?limit=5&sweep=1")
        assert root_res.status_code == 200, root_res.get_json()

    conn = points.get_db()
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM points_chain_transfer_requests WHERE request_uuid LIKE 'batch-sweep-transfer-%' AND status='pending'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert remaining == 0
    assert points.explorer_wallet(recipient_address)["wallet"]["points_balance"] == 150


def test_root_compact_transaction_list_runs_bounded_finality_sweep(tmp_path):
    app, points, _auto_ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        sender_wallet = create_official_hot_wallet(conn, user_id=2, chain_secret="test-secret", label="compact sender hot")
        recipient_address = "pc1" + ("8" * 48)
        conn.commit()
    finally:
        conn.close()

    points.record_transaction(
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="credit",
        amount=300,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="compact-batch-sweep-source",
        idempotency_key="explorer:test:compact-batch-sweep-source",
        reason="compact fund transfer source",
        actor={"id": 1, "username": "root", "role": "super_admin"},
    )

    current.update({"id": 2, "username": "test", "role": "user"})
    for index in range(5):
        res = client.post(
            "/api/points/transactions/submit",
            json={
                "source_wallet_address": sender_wallet["address"],
                "destination_wallet_address": recipient_address,
                "amount_points": 10,
                "fee_points": 1,
                "request_uuid": f"compact-batch-sweep-transfer-{index}",
                "memo": "compact batch sweep finality",
            },
        )
        assert res.status_code == 200, res.get_json()

    proved_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_transfer_requests SET created_at=? WHERE request_uuid LIKE 'compact-batch-sweep-transfer-%'",
            (proved_at,),
        )
        conn.commit()
    finally:
        conn.close()

    current.update({"id": 1, "username": "root", "role": "super_admin"})
    root_res = client.get("/api/points/transactions?limit=5&compact=1")
    assert root_res.status_code == 200, root_res.get_json()
    payload = root_res.get_json()
    assert payload["summary"]["compact"] is True
    assert payload["summary"]["maintenance_run"] is False
    assert payload["summary"]["batch_checked_count"] == 0
    assert payload["summary"]["batch_finalized_count"] == 0
    assert payload["summary"]["pending_count"] == 5
    assert all(item["status"] == "pending" for item in payload["transactions"])

    root_res = client.get("/api/points/transactions?limit=5&compact=1&sweep=1")
    assert root_res.status_code == 200, root_res.get_json()
    payload = root_res.get_json()
    assert payload["summary"]["compact"] is True
    assert payload["summary"]["maintenance_run"] is True
    assert payload["summary"]["batch_checked_count"] == 5
    assert payload["summary"]["batch_finalized_count"] == 5
    assert payload["summary"]["batch_confirmed_count"] == 5
    assert payload["summary"]["pending_count"] == 0
    assert all(item["status"] == "confirmed" for item in payload["transactions"])

    conn = points.get_db()
    try:
        remaining = conn.execute(
            "SELECT COUNT(*) FROM points_chain_transfer_requests WHERE request_uuid LIKE 'compact-batch-sweep-transfer-%' AND status='pending'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert remaining == 0


def test_transaction_management_remains_readable_in_safe_mode(tmp_path):
    app, points, _auto_ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        sender_wallet = create_official_hot_wallet(conn, user_id=2, chain_secret="test-secret", label="sender hot")
        recipient_address = "pc1" + ("6" * 48)
        conn.commit()
    finally:
        conn.close()

    points.record_transaction(
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="credit",
        amount=50,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="safe-mode-transfer-source",
        idempotency_key="explorer:test:safe-mode-transfer-source",
        reason="fund transfer source",
        actor={"id": 1, "username": "root", "role": "super_admin"},
    )

    current.update({"id": 2, "username": "test", "role": "user"})
    res = client.post(
        "/api/points/transactions/submit",
        json={
            "source_wallet_address": sender_wallet["address"],
                "destination_wallet_address": recipient_address,
            "amount_points": 10,
            "fee_points": 1,
            "request_uuid": "safe-mode-transfer",
            "memo": "safe mode read list",
        },
    )
    assert res.status_code == 200, res.get_json()
    tx_hash = res.get_json()["transaction_hash"]

    proved_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_transfer_requests SET created_at=? WHERE tx_group_hash=?",
            (proved_at, tx_hash),
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO points_chain_recovery_state
            (id, safe_mode, reason, verification_json, created_at, updated_at)
            VALUES (1, 1, 'qa_safe_mode', '{}', ?, ?)
            """,
            (proved_at, proved_at),
        )
        conn.commit()
    finally:
        conn.close()

    current.update({"id": 1, "username": "root", "role": "super_admin"})
    root_res = client.get("/api/points/transactions?limit=10")
    assert root_res.status_code == 200, root_res.get_json()
    payload = root_res.get_json()
    assert payload["warnings"] == ["chain_safe_mode_active_finalization_paused"]
    assert payload["recovery"]["safe_mode"] is True
    assert payload["summary"]["pending_count"] == 1
    assert payload["transactions"][0]["transaction_hash"] == tx_hash
    assert payload["transactions"][0]["status"] == "pending"
    assert payload["transactions"][0]["finality"]["finality_status"] == "proved"


def test_root_official_wallet_grant_creates_governed_multisig_transfer(tmp_path):
    app, points, _ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        root_wallet = create_official_hot_wallet(conn, user_id=1, chain_secret=points.chain_secret)
        recipient_wallet = create_official_hot_wallet(conn, user_id=2, chain_secret=points.chain_secret)
        manager_wallet = create_official_hot_wallet(conn, user_id=4, chain_secret=points.chain_secret)
        conn.commit()
    finally:
        conn.close()
    recipient_before = points.explorer_wallet(recipient_wallet["address"])["wallet"]["points_balance"]

    current.update({"id": 1, "username": "root", "role": "super_admin"})
    res = client.post(
        "/api/root/points/official-wallet/grant",
        json={
            "destination_wallet_address": recipient_wallet["address"],
            "amount": 8,
            "reason": "root pending proof",
            "request_uuid": "route-root-official-grant-1",
        },
    )
    assert res.status_code == 200, res.get_json()
    payload = res.get_json()
    proposal_uuid = payload["proposal"]["proposal_uuid"]

    assert payload["proposal"]["governance_domain"] == "OFFICIAL_TREASURY"
    assert payload["proposal"]["root_veto_allowed"] is True
    assert payload["proposal"]["multisig"]["required"] is True
    assert points.explorer_wallet(recipient_wallet["address"])["wallet"]["points_balance"] == recipient_before

    root_vote = client.post(f"/api/points/governance/proposals/{proposal_uuid}/vote", json={"vote": "yes"})
    assert root_vote.status_code == 200, root_vote.get_json()
    current.update({"id": 4, "username": "manager", "role": "manager"})
    manager_vote = client.post(f"/api/points/governance/proposals/{proposal_uuid}/vote", json={"vote": "yes"})
    assert manager_vote.status_code == 200, manager_vote.get_json()
    root_sign = None
    current.update({"id": 1, "username": "root", "role": "super_admin"})
    root_sign = client.post(f"/api/admin/points/governance/proposals/{proposal_uuid}/multisig-sign", json={"signer_wallet_address": root_wallet["address"]})
    assert root_sign.status_code == 200, root_sign.get_json()
    current.update({"id": 4, "username": "manager", "role": "manager"})
    manager_sign = client.post(f"/api/admin/points/governance/proposals/{proposal_uuid}/multisig-sign", json={"signer_wallet_address": manager_wallet["address"]})
    assert manager_sign.status_code == 200, manager_sign.get_json()
    assert manager_sign.get_json()["multisig"]["ready"] is True
    executed = client.post(f"/api/admin/points/governance/proposals/{proposal_uuid}/execute", json={})
    assert executed.status_code == 200, executed.get_json()
    tx_hash = executed.get_json()["result"]["transfer"]["transaction_hash"]

    current.update({"id": 2, "username": "test", "role": "user"})
    recipient_transactions = client.get("/api/points/transactions?limit=10").get_json()
    assert recipient_transactions["summary"]["pending_incoming_points"] == 0
    assert recipient_transactions["transactions"][0]["direction"] == "incoming"
    assert recipient_transactions["transactions"][0]["status"] == "confirmed"
    assert recipient_transactions["transactions"][0]["settlement_rail"] == "internal_hot_wallet"
    assert recipient_transactions["transactions"][0]["finality"]["finality_status"] == "internal_settled"
    assert recipient_transactions["transactions"][0]["transaction_hash"] == tx_hash

    conn = points.get_db()
    try:
        rows = conn.execute(
            "SELECT user_id, type, body FROM notifications WHERE type='points_chain_official_grant_completed' ORDER BY user_id"
        ).fetchall()
    finally:
        conn.close()
    assert [int(row["user_id"]) for row in rows] == [2, 4]
    assert all(tx_hash in row["body"] for row in rows)
    assert all("站內帳本即時" in row["body"] for row in rows)

    current.update({"id": 1, "username": "root", "role": "super_admin"})
    proved_transactions = client.get("/api/points/transactions?limit=10").get_json()
    assert proved_transactions["transactions"][0]["status"] == "confirmed"
    assert proved_transactions["transactions"][0]["finality"]["finality_status"] == "internal_settled"
    assert points.explorer_wallet(recipient_wallet["address"])["wallet"]["points_balance"] == recipient_before + 8


def test_manager_can_view_official_wallet_and_create_treasury_proposal(tmp_path):
    app, points, _ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    conn = points.get_db()
    try:
        points.ensure_schema(conn)
        create_official_hot_wallet(conn, user_id=1, chain_secret=points.chain_secret)
        create_official_hot_wallet(conn, user_id=4, chain_secret=points.chain_secret)
        recipient_wallet = create_official_hot_wallet(conn, user_id=3, chain_secret=points.chain_secret)
        conn.commit()
    finally:
        conn.close()

    current.update({"id": 4, "username": "manager", "role": "manager"})
    center_res = client.get("/api/admin/points/governance/treasury-signer-center?limit=20")
    assert center_res.status_code == 200, center_res.get_json()
    center = center_res.get_json()
    assert center["official_wallet"]["address"].startswith("pc0")
    assert center["official_wallet"]["spend_capability"] == "enabled"
    assert center["fund_addresses"]["official_treasury"] == center["official_wallet"]["address"]
    assert center["economy_layer"]["funds"]["official_treasury"]["address"] == center["official_wallet"]["address"]

    proposal_res = client.post(
        "/api/admin/points/governance/treasury-transfer",
        json={
            "destination_wallet_address": recipient_wallet["address"],
            "amount": 9,
            "reason": "manager official wallet disposition proposal",
            "reference": "manager-treasury-route-1",
        },
    )
    assert proposal_res.status_code == 200, proposal_res.get_json()
    proposal = proposal_res.get_json()["proposal"]
    assert proposal["governance_domain"] == "OFFICIAL_TREASURY"
    assert proposal["proposal_type"] == "official_treasury_operation"
    assert proposal["target_wallet_address"] == recipient_wallet["address"]
    assert proposal["requested_amount"] == 9
    assert proposal["root_veto_allowed"] is True
    assert proposal["multisig"]["required"] is True


def test_governance_admin_routes_reject_user_and_csrf_failures(tmp_path):
    app, points, _ledger, _manual_ledger, current = _build_app(tmp_path)
    client = app.test_client()
    current.update({"id": 2, "username": "test", "role": "user"})

    admin_routes = [
        ("/api/admin/points/governance/recovery-branch", {"incident_tx_hash": "tx", "reason": "blocked user emergency"}),
        ("/api/admin/points/governance/proposals/pcgov:missing/execute", {}),
        ("/api/admin/points/governance/proposals/pcgov:missing/multisig-sign", {"signer_wallet_address": "pc1" + "1" * 48}),
        ("/api/root/points/governance/proposals/pcgov:missing/veto", {"reason": "blocked user veto"}),
    ]
    for route, body in admin_routes:
        res = client.post(route, json=body)
        assert res.status_code == 403, (route, res.get_json())

    csrf_app, _csrf_points, _a, _b, csrf_current = _build_app(tmp_path / "csrf", require_csrf=_blocked_post_csrf_guard)
    csrf_client = csrf_app.test_client()
    csrf_current.update({"id": 1, "username": "root", "role": "super_admin"})
    csrf_res = csrf_client.post(
        "/api/root/points/governance/proposals/pcgov:missing/veto",
        json={"reason": "bad csrf should stop before service"},
    )
    assert csrf_res.status_code == 403
    assert csrf_res.get_json()["error"] == "csrf_invalid"

    root = {"id": 1, "username": "root", "role": "super_admin"}
    created = points.create_address_risk_proposal(
        actor=root,
        wallet_address="pc1" + ("7" * 48),
        reason="public proposal veto must be rejected",
        evidence=["tx-public-veto"],
    )
    with pytest.raises(PermissionError, match="not allowed"):
        points.veto_governance_proposal(actor=root, proposal_uuid=created["proposal"]["proposal_uuid"], reason="bad veto")
