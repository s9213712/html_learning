import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from services.points_chain import (
    BURN_WALLET_ADDRESS,
    BIRTHDAY_GIFT_POINTS,
    PointsLedgerService,
    USER_INITIAL_POINTS,
    address_from_public_key,
    bind_self_custody_wallet,
    create_official_hot_wallet,
    ensure_points_economy_schema,
    wallet_binding_payload,
    wallet_service_fee_payload,
    wallet_transaction_payload,
)

ROOT = Path(__file__).resolve().parents[2]
from services.points_chain.economy_layer import economy_fund_address
from services.points_chain.schema import canonical_json, compute_block_hash, compute_ledger_hash, merkle_root


def _db(tmp_path):
    path = tmp_path / "points.db"

    def get_db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # Phase 3: every connection must register app_mode() so the
        # BEFORE INSERT trigger on points_chain_blocks has something
        # to evaluate. These tests run as production-mode behavior.
        from services.platform.db_mode_triggers import register_app_mode_function
        register_app_mode_function(conn, mode_reader=lambda: "production")
        return conn

    conn = get_db()
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active')"
    )
    conn.execute(
        "INSERT INTO users (username, role, status) VALUES "
        "('alice', 'user', 'active'), "
        "('bob', 'manager', 'active'), "
        "('root', 'super_admin', 'active')"
    )
    ensure_points_economy_schema(conn)
    conn.commit()
    conn.close()
    return get_db


def _service(tmp_path):
    # Phase 7: chain writes require mode == 'production'. These tests
    # exercise the production-mode behavior of PointsChain; the new
    # production-only guard takes a mode_reader and refuses non-prod.
    return PointsLedgerService(
        get_db=_db(tmp_path),
        chain_secret="test-secret",
        backup_dir=tmp_path / "points_chain_backups",
        mode_reader=lambda: "production",
    )


def test_points_schema_adds_finance_hot_path_indexes(tmp_path):
    service = _service(tmp_path)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        ledger_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(points_ledger)").fetchall()}
        assert "idx_points_ledger_branch_user_id_desc" in ledger_indexes
        assert "idx_points_ledger_branch_action_user" in ledger_indexes

        bridge_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(points_chain_bridge_events)").fetchall()}
        assert "idx_points_chain_bridge_chain_tx" in bridge_indexes
        transfer_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(points_chain_transfer_requests)").fetchall()}
        assert "idx_points_chain_transfer_branch_status_created" in transfer_indexes
        service_fee_indexes = {row["name"] for row in conn.execute("PRAGMA index_list(points_service_fee_charges)").fetchall()}
        assert "idx_points_service_fee_branch_status_item" in service_fee_indexes

        plan = conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM points_chain_bridge_events
            WHERE chain=? AND chain_tx_hash=? AND chain_tx_hash<>''
            LIMIT 1
            """,
            ("points_chain_sim", "tx-finance-hot-path"),
        ).fetchall()
        details = " ".join(str(row["detail"]) for row in plan)
        assert "idx_points_chain_bridge_chain_tx" in details
    finally:
        conn.close()


def test_operations_control_snapshot_is_bounded_and_covers_initial_distribution(tmp_path):
    service = _service(tmp_path)
    snapshot = service.operations_control_snapshot()

    assert snapshot["snapshot_type"] == "points_operations_control"
    assert snapshot["bounded"] is True
    assert snapshot["initial_distribution"]["eligible"]["admins"] == 1
    assert snapshot["initial_distribution"]["eligible"]["users"] == 1
    assert snapshot["initial_distribution"]["missing"]["admin_initial_grants"] == 1
    assert snapshot["initial_distribution"]["missing"]["user_initial_grants"] == 1
    assert snapshot["service_fee_connection"]["catalog"]["total_items"] >= 1
    assert "private_chain" in snapshot
    assert "economy_model" in snapshot
    assert "exchange_operations" in snapshot
    assert "emergency" in snapshot
    assert snapshot["management_timing"]["bounded"] is True


def test_transfer_finality_observability_snapshot_is_bounded_and_non_mutating(tmp_path):
    service = _service(tmp_path)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        branch = service._canonical_branch_uuid(conn)
        conn.execute(
            """
            INSERT INTO points_chain_transfer_requests (
                request_uuid, chain_branch, request_hash, tx_group_hash,
                sender_user_id, recipient_user_id, source_wallet_address, destination_wallet_address,
                amount_points, fee_points, settlement_rail, chain_required, approval_required,
                network_fee_points, service_fee_points, status, created_at
            ) VALUES (?, ?, ?, ?, 1, 2, 'pc0:alice', 'pc1:bob', 50, 1, 'cold_chain', 1, 1, 1, 0, 'pending', ?)
            """,
            ("obs-pending", branch, "req-hash", "tx-hash", (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    snapshot = service.transfer_finality_observability_snapshot(recent_limit=25)

    assert snapshot["snapshot_type"] == "points_transfer_finality_observability"
    assert snapshot["bounded"] is True
    assert snapshot["recent_limit"] == 25
    assert snapshot["pending_queue"]["pending_count"] == 1
    assert snapshot["pending_queue"]["by_settlement_rail"]["cold_chain"]["request_count"] == 1
    assert snapshot["compact_sweep"]["maintenance_from_health_endpoint"] is False
    assert snapshot["compact_sweep"]["root_transaction_list_sweep_limit"] == 5
    assert snapshot["management_timing"]["bounded"] is True

    sweep = service.run_transfer_finality_sweep(actor={"id": 1, "username": "root", "role": "super_admin"}, limit=25)

    assert sweep["sweep_type"] == "points_transfer_finality_sweep"
    assert sweep["bounded"] is True
    assert sweep["limit"] == 25
    assert "sweep" in sweep
    assert "deposit_bridge" in sweep
    assert sweep["observability_marker"]["source"] == "root_finality_sweep_job"
    assert sweep["management_timing"]["bounded"] is True


def test_wallet_identity_materialized_balances_update_and_verify(tmp_path, monkeypatch):
    service = _service(tmp_path)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=125,
        action_type="materialized_seed",
        public_metadata={"source_fund_key": "mint"},
    )
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        hot = create_official_hot_wallet(conn, user_id=1, chain_secret=service.chain_secret)
        row = conn.execute(
            """
            SELECT *
            FROM points_wallet_identity_balances
            WHERE chain_branch='main' AND wallet_address=?
            """,
            (hot["address"],),
        ).fetchone()
        assert row is not None
        assert row["available_points"] == 125
        assert row["frozen_points"] == 0
        assert row["pending_outgoing_points"] == 0
        verify = service.verify_wallet_identity_balances(conn, "main", mode="full")
        assert verify["ok"], verify

        def fail_replay(*_args, **_kwargs):
            raise AssertionError("materialized balance read should not replay points_ledger")

        monkeypatch.setattr(service, "_ledger_wallet_flow_for_read", fail_replay)
        state = service._wallet_identity_balances_for_user(conn, 1, include_pending=False)
        assert state["source"] == "materialized_wallet_identity_balances"
        assert state["balances"][hot["address"]]["balance"] == 125
    finally:
        conn.close()


def _official_hot_wallet(service, user_id):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        wallet = create_official_hot_wallet(conn, user_id=user_id, chain_secret=service.chain_secret)
        conn.commit()
        return wallet
    finally:
        conn.close()


def _cold_wallet(service, user_id=1):
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = _public_jwk(private_key)
    address = address_from_public_key(public_jwk)
    bind_payload = wallet_binding_payload(
        user_id=user_id,
        wallet_type="self_custody_cold",
        address=address,
        public_key_jwk=public_jwk,
    )
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        wallet = bind_self_custody_wallet(
            conn,
            user_id=user_id,
            wallet_type="self_custody_cold",
            public_key_jwk=public_jwk,
            address=address,
            signature=_signature(private_key, bind_payload),
            backup_confirmed=True,
        )
        conn.commit()
        return wallet, private_key, address
    finally:
        conn.close()


def _record_pc1_canonical_credit(service, *, user_id=1, amount=10, action_type="test_pc1_credit"):
    _wallet, _private_key, address = _cold_wallet(service, user_id=user_id)
    return service.record_transaction(
        user_id=user_id,
        currency_type="points",
        direction="credit",
        amount=amount,
        action_type=action_type,
        public_metadata={
            "settlement_rail": "cold_chain",
            "chain_required": True,
            "approval_required": True,
            "network_fee_points": 0,
            "service_fee_points": 0,
            "source_fund_key": "mint",
            "destination_wallet_address": address,
        },
    )


def _b64url(data):
    return base64.urlsafe_b64encode(bytes(data)).decode("ascii").rstrip("=")


def _public_jwk(private_key):
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "EC",
        "crv": "P-256",
        "x": _b64url(numbers.x.to_bytes(32, "big")),
        "y": _b64url(numbers.y.to_bytes(32, "big")),
    }


def _signature(private_key, payload):
    der = private_key.sign(canonical_json(payload).encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    return _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def _force_request_proved(service, tx_hash):
    proved_at = (datetime.now(timezone.utc) - timedelta(seconds=600)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_transfer_requests SET created_at=? WHERE tx_group_hash=?",
            (proved_at, tx_hash),
        )
        conn.commit()
    finally:
        conn.close()


def _set_governance_timelock_ready(service, proposal_uuid):
    ready_at = "2026-01-01T00:00:00Z"
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
            (proposal_uuid,),
        ).fetchone()
        payload = json.loads(row["payload_json"] or "{}") if row else {}
        guard = payload.get("execution_guard") if isinstance(payload, dict) else None
        if isinstance(guard, dict):
            guard["timelock_until"] = ready_at
            guard["timelock_ends_at"] = ready_at
        execution_payload_hash = service._governance_execution_payload_hash(
            action_type=row["action_type"],
            governance_domain=row["governance_domain"],
            target_wallet_address=row["target_wallet_address"],
            target_address=row["target_address"],
            target_branch=row["target_branch"],
            requested_amount=row["requested_amount"],
            requested_asset=row["requested_asset"],
            payload=payload,
        )
        conn.execute(
            """
            UPDATE points_chain_governance_proposals
            SET timelock_until=?, timelock_ends_at=?, payload_json=?, execution_payload_hash=?
            WHERE proposal_uuid=?
            """,
            (
                ready_at,
                ready_at,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                execution_payload_hash,
                proposal_uuid,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _official_treasury_grant_via_governance(service, *, destination_wallet_address, amount, request_uuid, action_type="TREASURY_TRANSFER"):
    root = {"id": 3, "username": "root", "role": "super_admin"}
    manager = {"id": 2, "username": "bob", "role": "manager"}
    root_wallet = _official_hot_wallet(service, 3)
    manager_wallet = _official_hot_wallet(service, 2)
    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination_wallet_address,
        amount=amount,
        reason="governance official transfer",
        reference=request_uuid,
        action_type=action_type,
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes")
    _set_governance_timelock_ready(service, proposal_uuid)
    service.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    signed = service.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])
    assert signed["multisig"]["ready"] is True
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    return executed["result"]["transfer"], proposal_uuid


def test_points_transaction_updates_wallet_and_hash_chain(tmp_path):
    service = _service(tmp_path)

    first = service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=10,
        action_type="test_credit",
        idempotency_key="credit:1",
    )
    second = service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="debit",
        amount=3,
        action_type="test_debit",
        idempotency_key="debit:1",
    )

    assert first["wallet"]["points_balance"] == 10
    assert second["wallet"]["points_balance"] == 7
    assert second["ledger"]["currency_type"] == "points"
    assert second["ledger"]["previous_ledger_hash"] == first["ledger"]["ledger_hash"]
    assert service.verify_chain()["ok"] is True


def test_economy_stats_reports_member_circulating_supply(tmp_path):
    service = _service(tmp_path)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=100,
        action_type="admin_adjust_credit",
        idempotency_key="member:credit",
    )
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="debit",
        amount=30,
        action_type="spend:post_cost_standard",
        idempotency_key="member:debit",
    )
    service.record_transaction(
        user_id=3,
        currency_type="points",
        direction="credit",
        amount=50,
        action_type="admin_adjust_credit",
        idempotency_key="root:credit",
    )

    stats = service.economy_stats()
    circulation = stats["circulation"]
    bridge = stats["economy_layer"]["legacy_bridge"]

    assert circulation["outstanding_points"] == 120
    assert circulation["member_outstanding_points"] == 70
    assert circulation["root_outstanding_points"] == 50
    assert circulation["member_wallet_count"] == 1
    assert circulation["root_wallet_count"] == 1
    assert circulation["ledger_net_points"] == 120
    assert circulation["member_ledger_net_points"] == 70
    assert circulation["member_supply_gap_points"] == 0
    assert bridge["legacy_outstanding_points"] == 70
    assert bridge["member_internal_available_points"] == 70
    assert bridge["member_internal_frozen_points"] == 0
    assert bridge["member_internal_circulating_points"] == 70
    assert bridge["root_outstanding_points"] == 50
    assert bridge["root_internal_circulating_points"] == 50
    assert bridge["total_legacy_outstanding_points"] == 120
    assert bridge["total_internal_circulating_points"] == 120
    assert bridge["holder_circulating_breakdown"]["member_internal_circulating_points"] == 70
    assert bridge["unfunded_legacy_outstanding_points"] == 0
    assert bridge["promo_balance"] == 5_000_000
    assert bridge["promo_balance_after_required_debit"] == 5_000_000
    assert bridge["actual_supply_equation_gap_points"] == 0
    assert bridge["bridged_supply_equation_gap_points"] == 0
    assert bridge["bridged_supply_equation_balanced"] is True
    assert bridge["system_burn_sink_balance"] == stats["economy_layer"]["supply"]["burned_total"]
    assert bridge["system_mint_unissued_balance"] == stats["economy_layer"]["supply"]["mint_remaining"]
    assert bridge["pc0_platform_internal_fund_balance"] == (
        bridge["official_treasury_balance"] + bridge["exchange_fund_balance"] + bridge["promo_fund_balance"]
    )


def test_economy_stats_replays_official_hot_wallet_circulation(tmp_path):
    service = _service(tmp_path)
    hot_wallet = _official_hot_wallet(service, 1)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=250,
        action_type="admin_adjust_credit",
        idempotency_key="pc0-circulation:credit",
    )
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute("UPDATE points_wallets SET soft_balance=0, hard_balance=0, soft_frozen=0, hard_frozen=0")
        conn.commit()
    finally:
        conn.close()

    stats = service.economy_stats()
    circulation = stats["circulation"]
    bridge = stats["economy_layer"]["legacy_bridge"]

    assert circulation["member_outstanding_points"] == 250
    assert circulation["member_available_points"] == 250
    assert circulation["member_frozen_points"] == 0
    assert circulation["member_wallet_count"] == 1
    assert stats["wallets"]["source"] == "official_hot_wallet_replay"
    assert bridge["member_internal_circulating_points"] == 250
    assert bridge["holder_circulating_breakdown"]["member_internal_circulating_points"] == 250
    wallet = service.get_wallet(1)
    assert wallet["wallet_identity_balances"][hot_wallet["address"]]["points_balance"] == 250


def test_unknown_ledger_actions_do_not_guess_fund_flows(tmp_path):
    service = _service(tmp_path)

    tx = service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=100,
        action_type="legacy_unknown_credit",
        idempotency_key="unknown:credit",
    )

    stats = service.economy_stats()["economy_layer"]
    ledger = service.list_ledger(user_id=1, limit=1)[0]

    assert tx["created"] is True
    assert stats["funds"]["official_treasury"]["balance"] == 10_000_000
    assert stats["funds"]["promo_fund"]["balance"] == 5_000_000
    assert stats["funds"]["exchange_fund"]["balance"] == 5_000_000
    assert ledger["wallet_flow"]["walletized"] is False
    assert "不再自動套用基金" in ledger["wallet_flow"]["walletization_note"]


def test_official_wallet_grant_debits_official_fund_by_wallet_address(tmp_path):
    service = _service(tmp_path)
    destination = _official_hot_wallet(service, 2)

    result, _proposal_uuid = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=destination["address"],
        amount=10_000,
        request_uuid="official-wallet-grant-1",
    )

    stats = service.economy_stats()["economy_layer"]

    assert result["transaction"]["wallet_flow"]["destination_wallet_address"] == destination["address"]
    assert result["transaction"]["action_type"] == "official_wallet_grant"
    assert result["transaction"]["status"] == "confirmed"
    assert result["transaction"]["settlement_rail"] == "internal_hot_wallet"
    assert result["transaction"]["chain_required"] is False
    assert result["transaction"]["approval_required"] is False
    assert result["transaction"]["finality"]["finality_status"] == "internal_settled"
    assert service.explorer_wallet(destination["address"])["wallet"]["points_balance"] == 10_000
    proved = service.explorer_transaction(result["transaction_hash"])["transaction"]
    stats = service.economy_stats()["economy_layer"]
    ledger = service.list_ledger(user_id=2, limit=1)[0]

    assert proved["status"] == "confirmed"
    assert proved["finality"]["finality_status"] == "internal_settled"
    assert stats["funds"]["official_treasury"]["balance"] == 9_990_000
    assert stats["supply"]["circulating_supply"] == 10_000
    assert stats["supply_equation"]["official_treasury_balance"] == 9_990_000
    assert stats["supply_equation"]["total_legacy_outstanding_points"] == 10_000
    assert stats["supply_equation"]["actual_supply_equation_gap_points"] == 0
    assert ledger["wallet_flow"]["source_label"] == "官方 Treasury 錢包"
    assert ledger["action_type"] == "official_wallet_grant"
    assert ledger["wallet_flow"]["destination_label"] in {"用戶模擬鏈錢包", "Legacy 帳本身份"}
    assert ledger["wallet_flow"]["walletized"] is True


def test_official_treasury_replenishes_exchange_fund_as_internal_transaction(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 3, "username": "root", "role": "super_admin"}
    exchange_address = economy_fund_address(service.chain_secret, "exchange_fund")
    before = service.economy_stats()["economy_layer"]

    result, _proposal_uuid = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=exchange_address,
        amount=123,
        request_uuid="official-fund-transfer-exchange-1",
        action_type="EXCHANGE_FUND_REPLENISH",
    )
    settled = service.economy_stats()["economy_layer"]
    root_transactions = service.list_wallet_transactions(user_id=3, actor=actor)

    assert result["created"] is True
    assert result["destination_fund_key"] == "exchange_fund"
    assert result["transaction"]["action_type"] == "official_fund_transfer"
    assert result["transaction"]["status"] == "confirmed"
    assert result["transaction"]["settlement_rail"] == "internal_hot_wallet"
    assert result["transaction"]["chain_required"] is False
    assert result["transaction"]["approval_required"] is False
    assert result["transaction"]["finality"]["finality_status"] == "internal_settled"
    assert result["transaction"]["wallet_flow"]["destination_fund_key"] == "exchange_fund"
    assert result["wallet"] is None
    assert settled["funds"]["exchange_fund"]["balance"] == before["funds"]["exchange_fund"]["balance"] + 123
    assert root_transactions["transactions"][0]["direction"] == "official_fund_transfer"
    assert root_transactions["transactions"][0]["status"] == "confirmed"

    proved = service.explorer_transaction(result["transaction_hash"])["transaction"]
    after = service.economy_stats()["economy_layer"]

    assert proved["status"] == "confirmed"
    assert proved["action_type"] == "official_fund_transfer"
    assert proved["finality"]["finality_status"] == "internal_settled"
    assert after["funds"]["exchange_fund"]["balance"] == before["funds"]["exchange_fund"]["balance"] + 123
    assert after["funds"]["official_treasury"]["balance"] == before["funds"]["official_treasury"]["balance"] - 123
    assert after["supply_equation"]["actual_supply_equation_gap_points"] == 0
    assert service.verify_chain()["ok"] is True


def test_wallet_transfer_to_unowned_address_confirms_and_balance_follows_address(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 1, "username": "alice", "role": "user"}
    source = _official_hot_wallet(service, 1)
    destination = "pc1" + "8" * 48
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=50,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-unowned-transfer",
        idempotency_key="fund-unowned-transfer",
        actor={"id": 3, "username": "root", "role": "super_admin"},
    )

    result = service.submit_wallet_transaction(
        actor=actor,
        source_wallet_address=source["address"],
        destination_wallet_address=destination,
        amount_points=12,
        fee_points=2,
        request_uuid="transfer-to-unowned-address-1",
        memo="send to public address",
    )
    pending_sender = service.get_wallet(1)

    assert result["created"] is True
    assert result["transaction"]["status"] == "pending"
    assert result["transaction"]["wallet_flow"]["destination_unowned"] is True
    assert result["warnings"] == []
    assert pending_sender["wallet_identity_balances"][source["address"]]["points_balance"] == 36
    assert pending_sender["wallet_identity_balances"][source["address"]]["points_frozen"] == 14
    assert service.explorer_wallet(destination)["wallet"]["points_balance"] == 0
    sender_transactions = service.list_wallet_transactions(user_id=1, actor=actor)
    assert sender_transactions["transactions"][0]["direction"] == "outgoing"
    assert sender_transactions["summary"]["pending_outgoing_points"] == 14

    _force_request_proved(service, result["transaction_hash"])
    proved = service.explorer_transaction(result["transaction_hash"])["transaction"]
    final_sender = service.get_wallet(1)
    destination_wallet = service.explorer_wallet(destination)["wallet"]

    assert proved["status"] == "confirmed"
    assert proved["transfer_ledgers"]["transfer_in_ledger_uuid"] == ""
    assert final_sender["wallet_identity_balances"][source["address"]]["points_balance"] == 36
    assert final_sender["wallet_identity_balances"][source["address"]]["points_frozen"] == 0
    assert destination_wallet["points_balance"] == 12
    assert destination_wallet["received_tx_count"] == 1
    assert destination_wallet["recent_transactions"][0]["transaction_hash"] == result["transaction_hash"]
    supply_equation = service.economy_stats()["economy_layer"]["supply_equation"]
    external_breakdown = supply_equation["economy_external_balance_breakdown"]
    bridge_flow = supply_equation["bridge_flow_totals"]
    assert supply_equation["off_wallet_economy_external_points"] == 12
    assert supply_equation["residual_off_wallet_economy_external_points"] == 12
    assert supply_equation["ledger_vs_economy_external_gap_points"] == 0
    assert external_breakdown["pc1_unbound_points"] == 12
    assert external_breakdown["cold_chain_or_bridge_external_points"] == 12
    assert bridge_flow["hot_to_cold_confirmed_points"] == 12
    assert bridge_flow["hot_to_cold_network_fee_points"] == 2
    assert bridge_flow["economy_external_flow_net_points"] == 12
    assert bridge_flow["economy_flow_reconciliation_gap_points"] == 0
    assert service.verify_chain()["ok"] is True


def test_wallet_transfer_direct_to_pc0_is_rejected_with_bridge_guidance(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 1, "username": "alice", "role": "user"}
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = _public_jwk(private_key)
    source_address = address_from_public_key(public_jwk)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        bind_self_custody_wallet(
            conn,
            user_id=1,
            wallet_type="self_custody_cold",
            public_key_jwk=public_jwk,
            address=source_address,
            signature=_signature(private_key, wallet_binding_payload(
                user_id=1,
                wallet_type="self_custody_cold",
                address=source_address,
                public_key_jwk=public_jwk,
            )),
            backup_confirmed=True,
        )
        conn.commit()
    finally:
        conn.close()
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=50,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-pc0-reject",
        idempotency_key="fund-pc0-reject",
        actor={"id": 3, "username": "root", "role": "super_admin"},
    )

    with pytest.raises(ValueError, match="pc0 official hot wallets are internal ledger addresses"):
        service.submit_wallet_transaction(
            actor=actor,
            source_wallet_address=source_address,
            destination_wallet_address="pc0" + ("8" * 48),
            amount_points=12,
            fee_points=2,
            request_uuid="direct-chain-to-pc0-must-fail",
            memo="must use deposit bridge",
        )

    conn = service.get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM points_chain_transfer_requests WHERE request_uuid='direct-chain-to-pc0-must-fail'"
        ).fetchone()
    finally:
        conn.close()
    assert row is None


def test_pc0_to_pc0_wallet_transfer_is_immediate_fee_free_internal_ledger(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 1, "username": "alice", "role": "user"}
    source = _official_hot_wallet(service, 1)
    destination = _official_hot_wallet(service, 2)
    assert source["address"].startswith("pc0")
    assert destination["address"].startswith("pc0")
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=80,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-pc0-internal-transfer",
        idempotency_key="fund-pc0-internal-transfer",
        actor={"id": 3, "username": "root", "role": "super_admin"},
    )

    result = service.submit_wallet_transaction(
        actor=actor,
        source_wallet_address=source["address"],
        destination_wallet_address=destination["address"],
        amount_points=25,
        fee_points=99,
        request_uuid="pc0-internal-transfer-1",
        memo="internal transfer",
    )

    assert result["transaction"]["status"] == "confirmed"
    assert result["transaction"]["settlement_rail"] == "internal_hot_wallet"
    assert result["transaction"]["fee_points"] == 0
    assert result["transaction"]["chain_required"] is False
    assert result["transaction"]["approval_required"] is False
    assert result["transfer_out_ledger"]["wallet_flow"]["settlement_rail"] == "internal_hot_wallet"
    assert result["transfer_in_ledger"]["wallet_flow"]["settlement_rail"] == "internal_hot_wallet"
    assert service.get_wallet(1)["wallet_identity_balances"][source["address"]]["points_balance"] == 55
    assert service.get_wallet(2)["wallet_identity_balances"][destination["address"]]["points_balance"] == 25
    supply_equation = service.economy_stats()["economy_layer"]["supply_equation"]
    external_breakdown = supply_equation["economy_external_balance_breakdown"]
    assert external_breakdown["pc0_member_points"] == 80
    assert external_breakdown["nonzero_address_count"] == 2
    assert service.verify_chain()["ok"] is True


def test_pc0_internal_transfer_recipient_can_later_withdraw_without_negative_economy_replay(tmp_path):
    service = _service(tmp_path)
    sender_actor = {"id": 1, "username": "alice", "role": "user"}
    recipient_actor = {"id": 2, "username": "bob", "role": "user"}
    source = _official_hot_wallet(service, 1)
    recipient_hot = _official_hot_wallet(service, 2)
    cold_destination = "pc1" + "9" * 48
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=80,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-pc0-withdraw-after-internal",
        idempotency_key="fund-pc0-withdraw-after-internal",
        actor={"id": 3, "username": "root", "role": "super_admin"},
    )

    internal = service.submit_wallet_transaction(
        actor=sender_actor,
        source_wallet_address=source["address"],
        destination_wallet_address=recipient_hot["address"],
        amount_points=25,
        fee_points=0,
        request_uuid="pc0-internal-before-withdraw",
        memo="internal first",
    )
    outgoing = service.submit_wallet_transaction(
        actor=recipient_actor,
        source_wallet_address=recipient_hot["address"],
        destination_wallet_address=cold_destination,
        amount_points=25,
        fee_points=0,
        request_uuid="pc0-recipient-withdraw-after-internal",
        memo="withdraw after internal",
    )
    _force_request_proved(service, outgoing["transaction_hash"])
    proved = service.explorer_transaction(outgoing["transaction_hash"])["transaction"]

    assert internal["transaction"]["status"] == "confirmed"
    assert proved["status"] == "confirmed"
    assert service.explorer_wallet(recipient_hot["address"])["wallet"]["points_balance"] == 0
    assert service.explorer_wallet(cold_destination)["wallet"]["points_balance"] == 25
    supply_equation = service.economy_stats()["economy_layer"]["supply_equation"]
    external_breakdown = supply_equation["economy_external_balance_breakdown"]
    bridge_flow = supply_equation["bridge_flow_totals"]
    assert external_breakdown["pc0_member_points"] == 55
    assert external_breakdown["pc1_unbound_points"] == 25
    assert external_breakdown["nonzero_address_count"] == 2
    assert bridge_flow["hot_to_cold_confirmed_points"] == 25
    assert bridge_flow["economy_external_flow_net_points"] == 25
    assert bridge_flow["economy_flow_reconciliation_gap_points"] == 0
    assert service.verify_chain()["ok"] is True


def test_walletized_backfill_repairs_legacy_missing_internal_transfer_event(tmp_path):
    service = _service(tmp_path)
    sender_actor = {"id": 1, "username": "alice", "role": "user"}
    recipient_actor = {"id": 2, "username": "bob", "role": "user"}
    source = _official_hot_wallet(service, 1)
    recipient_hot = _official_hot_wallet(service, 2)
    cold_destination = "pc1" + "8" * 48
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=80,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-legacy-missing-internal-backfill",
        idempotency_key="fund-legacy-missing-internal-backfill",
        actor={"id": 3, "username": "root", "role": "super_admin"},
    )

    internal = service.submit_wallet_transaction(
        actor=sender_actor,
        source_wallet_address=source["address"],
        destination_wallet_address=recipient_hot["address"],
        amount_points=25,
        fee_points=0,
        request_uuid="legacy-missing-pc0-internal-before-withdraw",
        memo="legacy internal first",
    )
    missing_idempotency = f"walletized_ledger:{internal['transfer_out_ledger']['ledger_uuid']}"
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute("DROP TRIGGER trg_points_economy_events_no_delete")
        conn.execute("DELETE FROM points_economy_events WHERE idempotency_key=?", (missing_idempotency,))
        conn.commit()
    finally:
        conn.close()

    outgoing = service.submit_wallet_transaction(
        actor=recipient_actor,
        source_wallet_address=recipient_hot["address"],
        destination_wallet_address=cold_destination,
        amount_points=25,
        fee_points=0,
        request_uuid="legacy-missing-pc0-recipient-withdraw-after-internal",
        memo="withdraw after legacy internal",
    )
    _force_request_proved(service, outgoing["transaction_hash"])
    service.explorer_transaction(outgoing["transaction_hash"])

    stats = service.economy_stats()["economy_layer"]
    assert stats["walletization_backfill"]["created"] >= 1
    external_breakdown = stats["supply_equation"]["economy_external_balance_breakdown"]
    assert external_breakdown["pc0_member_points"] == 55
    assert external_breakdown["pc1_unbound_points"] == 25
    assert service.verify_chain()["ok"] is True


def test_deposit_bridge_credits_pc0_hot_wallet_without_chain_destination_pc0(tmp_path):
    service = _service(tmp_path)
    wallet = service.get_wallet(2)
    hot_address = wallet["active_wallet_address"]
    deposit_address = wallet["deposit_address"]
    assert hot_address.startswith("pc0")
    assert deposit_address.startswith("pc1")

    result = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-1",
    )

    assert result["bridge_event"]["destination_address"] == deposit_address
    assert result["bridge_event"]["hot_wallet_address"] == hot_address
    assert result["ledger"]["action_type"] == "deposit_credit"
    assert result["ledger"]["public_metadata"]["settlement_rail"] == "deposit_bridge_credit"
    assert result["ledger"]["public_metadata"]["destination_wallet_address"] == hot_address
    assert result["ledger"]["public_metadata"]["deposit_address"] == deposit_address
    assert service.get_wallet(2)["wallet_identity_balances"][hot_address]["points_balance"] == 70
    bridge_flow = service.economy_stats()["economy_layer"]["supply_equation"]["bridge_flow_totals"]
    assert bridge_flow["deposit_credited_points"] == 70
    assert bridge_flow["pc0_request_net_outflow_points"] == -70

    duplicate = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-1",
    )

    assert duplicate["created"] is False
    assert duplicate["ledger"]["ledger_uuid"] == result["ledger"]["ledger_uuid"]
    assert service.get_wallet(2)["wallet_identity_balances"][hot_address]["points_balance"] == 70


def test_deposit_bridge_pending_later_confirmed_credits_once(tmp_path):
    service = _service(tmp_path)
    wallet = service.get_wallet(2)
    hot_address = wallet["active_wallet_address"]
    deposit_address = wallet["deposit_address"]

    pending = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-pending-then-confirmed",
        confirmations=2,
        required_confirmations=20,
    )
    confirmed = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-pending-then-confirmed",
        confirmations=20,
        required_confirmations=20,
        risk_status="accepted",
    )
    duplicate = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-pending-then-confirmed",
        confirmations=22,
        required_confirmations=20,
        risk_status="accepted",
    )

    assert pending["credited"] is False
    assert confirmed["created"] is False
    assert confirmed["credited"] is True
    assert confirmed["ledger_created"] is True
    assert duplicate["credited"] is True
    assert duplicate["ledger"]["ledger_uuid"] == confirmed["ledger"]["ledger_uuid"]
    assert service.get_wallet(2)["wallet_identity_balances"][hot_address]["points_balance"] == 70


def test_deposit_bridge_review_later_accepted_credits_once(tmp_path):
    service = _service(tmp_path)
    wallet = service.get_wallet(2)
    hot_address = wallet["active_wallet_address"]
    deposit_address = wallet["deposit_address"]

    review = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-review-then-accepted",
        confirmations=20,
        required_confirmations=20,
        risk_status="review",
    )
    accepted = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-review-then-accepted",
        confirmations=20,
        required_confirmations=20,
        risk_status="accepted",
    )
    duplicate = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-review-then-accepted",
        confirmations=20,
        required_confirmations=20,
        risk_status="accepted",
    )

    assert review["credited"] is False
    assert accepted["credited"] is True
    assert accepted["ledger_created"] is True
    assert duplicate["ledger"]["ledger_uuid"] == accepted["ledger"]["ledger_uuid"]
    assert service.get_wallet(2)["wallet_identity_balances"][hot_address]["points_balance"] == 70


def test_deposit_address_active_row_is_not_silently_replaced(tmp_path):
    service = _service(tmp_path)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        first = service.ensure_user_deposit_address(conn, 1)
        rotated_seed_attempt = service._ensure_user_deposit_address_locked(
            conn,
            1,
            vault_key="future-vault",
            version=2,
        )
        conn.commit()
    finally:
        conn.close()

    assert rotated_seed_attempt["id"] == first["id"]
    assert rotated_seed_attempt["address"] == first["address"]
    assert rotated_seed_attempt["vault_key"] == first["vault_key"]


def test_deposit_bridge_rejects_wrong_destination_address(tmp_path):
    service = _service(tmp_path)
    wallet = service.get_wallet(2)
    hot_address = wallet["active_wallet_address"]
    wrong_destination = "pc1" + ("e" * 48)

    with pytest.raises(ValueError, match="deposit destination does not match"):
        service.confirm_deposit_to_hot_wallet(
            actor={"id": 3, "username": "root", "role": "super_admin"},
            user_id=2,
            source_address="pc1" + ("d" * 48),
            destination_address=wrong_destination,
            amount_points=70,
            chain_tx_hash="deposit-chain-tx-wrong-destination",
        )

    assert service.get_wallet(2)["wallet_identity_balances"][hot_address]["points_balance"] == 0


def test_bridge_flow_reconstruction_does_not_double_count_pc0_fund_wallets(tmp_path):
    service = _service(tmp_path)
    service.economy_stats()
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        service.append_trading_reserve_economy_event(
            conn,
            reserve_event_uuid="reserve-lend-with-pc0-fund-source",
            delta=-50,
            event_type="margin_principal_lent",
            source_user_id=1,
        )
        service.append_trading_reserve_economy_event(
            conn,
            reserve_event_uuid="reserve-repay-with-pc0-fund-destination",
            delta=20,
            event_type="margin_principal_repaid",
            source_user_id=1,
        )
        conn.commit()
    finally:
        conn.close()

    bridge_flow = service.economy_stats()["economy_layer"]["supply_equation"]["bridge_flow_totals"]

    assert bridge_flow["economy_hot_to_external_points"] == 50
    assert bridge_flow["economy_fund_to_external_points"] == 50
    assert bridge_flow["economy_external_to_hot_points"] == 20
    assert bridge_flow["economy_external_to_fund_points"] == 20
    assert bridge_flow["economy_external_address_in_points"] == 50
    assert bridge_flow["economy_external_address_out_points"] == 20
    assert bridge_flow["economy_external_flow_net_points"] == 30
    assert bridge_flow["current_cold_chain_or_bridge_external_points"] == 30
    assert bridge_flow["economy_flow_reconciliation_gap_points"] == 0
    assert service.verify_chain()["financial_invariants"]["ok"] is True


def test_deposit_bridge_pending_confirmations_do_not_credit_wallet(tmp_path):
    service = _service(tmp_path)
    wallet = service.get_wallet(2)
    hot_address = wallet["active_wallet_address"]
    deposit_address = wallet["deposit_address"]

    result = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-pending",
        confirmations=3,
        required_confirmations=20,
    )

    assert result["created"] is True
    assert result["credited"] is False
    assert result["reason"] == "confirmations_pending"
    assert result["bridge_event"]["status"] == "pending"
    assert result["bridge_event"]["internal_ledger_uuid"] == ""
    assert result["ledger"] is None
    assert service.get_wallet(2)["wallet_identity_balances"][hot_address]["points_balance"] == 0


def test_deposit_bridge_risk_review_does_not_credit_wallet(tmp_path):
    service = _service(tmp_path)
    wallet = service.get_wallet(2)
    hot_address = wallet["active_wallet_address"]
    deposit_address = wallet["deposit_address"]

    result = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-review",
        confirmations=20,
        required_confirmations=20,
        risk_status="review",
    )

    assert result["created"] is True
    assert result["credited"] is False
    assert result["reason"] == "risk_review"
    assert result["bridge_event"]["risk_status"] == "review"
    assert result["bridge_event"]["status"] == "pending"
    assert result["ledger"] is None
    assert service.get_wallet(2)["wallet_identity_balances"][hot_address]["points_balance"] == 0


def test_deposit_bridge_blocked_does_not_credit_wallet_or_retry_to_credit(tmp_path):
    service = _service(tmp_path)
    wallet = service.get_wallet(2)
    hot_address = wallet["active_wallet_address"]
    deposit_address = wallet["deposit_address"]

    blocked = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-blocked",
        confirmations=20,
        required_confirmations=20,
        risk_status="blocked",
    )
    retry = service.confirm_deposit_to_hot_wallet(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=deposit_address,
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-blocked",
        confirmations=20,
        required_confirmations=20,
        risk_status="accepted",
    )

    assert blocked["credited"] is False
    assert blocked["bridge_event"]["status"] == "failed"
    assert blocked["reason"] == "risk_blocked"
    assert retry["credited"] is False
    assert retry["bridge_event"]["status"] == "failed"
    assert retry["ledger"] is None
    assert service.get_wallet(2)["wallet_identity_balances"][hot_address]["points_balance"] == 0


def test_cold_chain_transfer_to_deposit_address_auto_credits_pc0_and_notifies(tmp_path):
    service = _service(tmp_path)
    sender = {"id": 1, "username": "alice", "role": "user"}
    root = {"id": 3, "username": "root", "role": "super_admin"}
    sender_hot = _official_hot_wallet(service, 1)
    recipient_wallet = service.get_wallet(2)
    recipient_hot = recipient_wallet["active_wallet_address"]
    recipient_deposit = recipient_wallet["deposit_address"]
    cold_key = ec.generate_private_key(ec.SECP256R1())
    cold_public_jwk = _public_jwk(cold_key)
    cold_address = address_from_public_key(cold_public_jwk)
    bind_payload = wallet_binding_payload(
        user_id=1,
        wallet_type="self_custody_cold",
        address=cold_address,
        public_key_jwk=cold_public_jwk,
    )
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        cold_wallet = bind_self_custody_wallet(
            conn,
            user_id=1,
            wallet_type="self_custody_cold",
            public_key_jwk=cold_public_jwk,
            address=cold_address,
            signature=_signature(cold_key, bind_payload),
            backup_confirmed=True,
        )
        conn.commit()
    finally:
        conn.close()
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=200,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-hot-before-cold-deposit-bridge",
        idempotency_key="fund-hot-before-cold-deposit-bridge",
        actor=root,
    )

    withdrawal = service.submit_wallet_transaction(
        actor=sender,
        source_wallet_address=sender_hot["address"],
        destination_wallet_address=cold_address,
        amount_points=100,
        fee_points=2,
        request_uuid="hot-to-cold-before-deposit-bridge",
    )
    _force_request_proved(service, withdrawal["transaction_hash"])
    service.explorer_transaction(withdrawal["transaction_hash"])
    assert service.get_wallet(1)["wallet_identity_balances"][cold_address]["points_balance"] == 100

    request_uuid = "cold-to-user-deposit-auto-bridge"
    signature_payload = wallet_transaction_payload(
        user_id=1,
        source_wallet_address=cold_address,
        destination_wallet_address=recipient_deposit,
        amount_points=70,
        fee_points=3,
        request_uuid=request_uuid,
        memo="deposit bridge",
        chain_branch="main",
        signer_key_id=cold_wallet["public_key_hash"],
    )
    pending = service.submit_wallet_transaction(
        actor=sender,
        source_wallet_address=cold_address,
        destination_wallet_address=recipient_deposit,
        amount_points=70,
        fee_points=3,
        request_uuid=request_uuid,
        memo="deposit bridge",
        signature=_signature(cold_key, signature_payload),
    )

    assert pending["transaction"]["status"] == "pending"
    assert pending["transaction"]["wallet_flow"]["destination_unowned"] is False
    _force_request_proved(service, pending["transaction_hash"])
    proved = service.explorer_transaction(pending["transaction_hash"])["transaction"]
    recipient_after = service.get_wallet(2)
    sender_after = service.get_wallet(1)

    assert proved["status"] == "confirmed"
    assert proved["transfer_ledgers"]["transfer_in_ledger_uuid"]
    assert recipient_after["wallet_identity_balances"][recipient_hot]["points_balance"] == 70
    assert sender_after["wallet_identity_balances"][cold_address]["points_balance"] == 27
    conn = service.get_db()
    try:
        bridge = conn.execute(
            "SELECT * FROM points_chain_bridge_events WHERE chain_tx_hash=?",
            (pending["transaction_hash"],),
        ).fetchone()
        note = conn.execute(
            """
            SELECT * FROM notifications
            WHERE user_id=2 AND type='points_chain_deposit_bridge_credited'
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()
    assert bridge is not None
    assert bridge["status"] == "credited"
    assert bridge["destination_address"] == recipient_deposit
    assert bridge["hot_wallet_address"] == recipient_hot
    assert int(bridge["network_fee_points"] or 0) == 3
    assert note is not None
    assert pending["transaction_hash"] in note["body"]
    flow = service.economy_stats()["economy_layer"]["supply_equation"]["bridge_flow_totals"]
    assert flow["hot_to_cold_confirmed_points"] == 100
    assert flow["deposit_credited_points"] == 70
    assert flow["pc0_request_net_outflow_points"] == 30


def test_confirmed_deposit_address_transfer_reconciles_missing_bridge_event(tmp_path):
    service = _service(tmp_path)
    sender = {"id": 1, "username": "alice", "role": "user"}
    root = {"id": 3, "username": "root", "role": "super_admin"}
    source = _official_hot_wallet(service, 1)
    recipient_hot = _official_hot_wallet(service, 2)
    legacy_deposit_address = "pc1" + ("9" * 48)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=120,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-legacy-confirmed-deposit-transfer",
        idempotency_key="fund-legacy-confirmed-deposit-transfer",
        actor=root,
    )
    transfer = service.submit_wallet_transaction(
        actor=sender,
        source_wallet_address=source["address"],
        destination_wallet_address=legacy_deposit_address,
        amount_points=60,
        fee_points=2,
        request_uuid="legacy-confirmed-deposit-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    service.explorer_transaction(transfer["transaction_hash"])
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        assert conn.execute(
            "SELECT 1 FROM points_chain_bridge_events WHERE chain_tx_hash=?",
            (transfer["transaction_hash"],),
        ).fetchone() is None
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        conn.execute(
            """
            INSERT INTO points_chain_deposit_addresses (
                user_id, chain, address, vault_key, status, metadata_json, created_at, updated_at
            ) VALUES (2, 'points_chain_sim', ?, 'legacy-test', 'active', '{}', ?, ?)
            """,
            (legacy_deposit_address, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    wallet = service.get_wallet(2)

    assert wallet["wallet_identity_balances"][recipient_hot["address"]]["points_balance"] == 60
    conn = service.get_db()
    try:
        req = conn.execute(
            "SELECT recipient_user_id, destination_unowned, transfer_in_ledger_uuid FROM points_chain_transfer_requests WHERE tx_group_hash=?",
            (transfer["transaction_hash"],),
        ).fetchone()
        bridge_count = conn.execute(
            "SELECT COUNT(*) AS count FROM points_chain_bridge_events WHERE chain_tx_hash=? AND status='credited'",
            (transfer["transaction_hash"],),
        ).fetchone()["count"]
    finally:
        conn.close()
    assert int(req["recipient_user_id"]) == 2
    assert int(req["destination_unowned"]) == 0
    assert req["transfer_in_ledger_uuid"]
    assert bridge_count == 1


def test_pc0_to_pc1_wallet_transfer_creates_withdrawal_bridge_lock(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 1, "username": "alice", "role": "user"}
    source = _official_hot_wallet(service, 1)
    destination = "pc1" + ("f" * 48)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=100,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-withdrawal-lock",
        idempotency_key="fund-withdrawal-lock",
        actor={"id": 3, "username": "root", "role": "super_admin"},
    )

    result = service.submit_wallet_transaction(
        actor=actor,
        source_wallet_address=source["address"],
        destination_wallet_address=destination,
        amount_points=40,
        fee_points=2,
        request_uuid="pc0-withdrawal-lock-1",
        memo="withdraw to cold",
    )

    assert result["transaction"]["status"] == "pending"
    assert result["transaction"]["settlement_rail"] == "withdrawal_bridge_lock"
    assert result["transaction"]["chain_required"] is True
    assert result["transaction"]["approval_required"] is True
    assert result["transaction"]["network_fee_points"] == 2
    assert result["transfer_out_ledger"] is None
    wallet = service.get_wallet(1)["wallet_identity_balances"][source["address"]]
    assert wallet["points_balance"] == 58
    assert wallet["points_frozen"] == 42


def test_schema_integrity_validator_reports_pc0_bridge_and_transfer_corruption(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 1, "username": "alice", "role": "user"}
    source = _official_hot_wallet(service, 1)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=100,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-schema-integrity",
        idempotency_key="fund-schema-integrity",
        actor={"id": 3, "username": "root", "role": "super_admin"},
    )
    request = service.submit_wallet_transaction(
        actor=actor,
        source_wallet_address=source["address"],
        destination_wallet_address="pc1" + ("a" * 48),
        amount_points=10,
        fee_points=1,
        request_uuid="schema-integrity-corrupt-transfer",
    )

    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            """
            UPDATE points_chain_transfer_requests
            SET settlement_rail='bad_rail',
                chain_required=2,
                approval_required=-1,
                network_fee_points=-7,
                service_fee_points=-3
            WHERE tx_group_hash=?
            """,
            (request["transaction_hash"],),
        )
        conn.execute(
            """
            INSERT INTO points_chain_bridge_events (
                bridge_uuid, bridge_type, user_id, chain, chain_tx_hash,
                source_address, destination_address, hot_wallet_address, amount_points,
                network_fee_points, confirmations, required_confirmations, risk_status,
                status, internal_ledger_uuid, metadata_json, created_at, updated_at
            ) VALUES ('', 'deposit', 1, 'points_chain_sim', 'schema-integrity-empty-bridge-uuid',
                'pc1bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                'pc1cccccccccccccccccccccccccccccccccccccccccccccccc',
                ?, 1, 0, 0, 20, 'accepted', 'pending', '', '{}', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (source["address"],),
        )
        conn.commit()
    finally:
        conn.close()

    report = service.schema_integrity_report()
    error_types = {item["type"] for item in report["errors"]}

    assert report["ok"] is False
    assert "schema_invalid_settlement_rail" in error_types
    assert "schema_invalid_bool" in error_types
    assert "schema_negative_fee" in error_types
    assert "schema_empty_bridge_uuid" in error_types
    verify = service.verify_chain()
    verify_types = {item["type"] for item in verify["errors"]}
    assert error_types.issubset(verify_types)


def test_wallet_transfer_to_burn_address_burns_amount_and_fee(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 1, "username": "alice", "role": "user"}
    source = _official_hot_wallet(service, 1)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=50,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-burn-transfer",
        idempotency_key="fund-burn-transfer",
        actor={"id": 3, "username": "root", "role": "super_admin"},
    )
    before = service.economy_stats()["economy_layer"]

    result = service.submit_wallet_transaction(
        actor=actor,
        source_wallet_address=source["address"],
        destination_wallet_address=BURN_WALLET_ADDRESS,
        amount_points=12,
        fee_points=2,
        request_uuid="transfer-to-burn-address-1",
        memo="burn points",
    )

    assert result["created"] is True
    assert result["transaction"]["status"] == "confirmed"
    assert result["transaction"]["settlement_rail"] == "internal_system_burn"
    assert result["transaction"]["chain_required"] is False
    assert result["transaction"]["network_fee_points"] == 0
    assert result["transaction"]["fee_points"] == 0
    assert result["warnings"] == []

    proved = service.explorer_transaction(result["transaction_hash"])["transaction"]
    final_sender = service.get_wallet(1)
    burn_wallet = service.explorer_wallet(BURN_WALLET_ADDRESS)["wallet"]
    after = service.economy_stats()["economy_layer"]

    assert proved["status"] == "confirmed"
    assert proved["transfer_ledgers"]["transfer_in_ledger_uuid"] == ""
    assert final_sender["wallet_identity_balances"][source["address"]]["points_balance"] == 38
    assert final_sender["wallet_identity_balances"][source["address"]]["points_frozen"] == 0
    assert after["supply"]["burned_total"] == before["supply"]["burned_total"] + 12
    assert after["supply"]["active_supply"] == before["supply"]["active_supply"] - 12
    assert after["funds"]["burn"]["balance"] == before["funds"]["burn"]["balance"] + 12
    assert after["supply_equation"]["system_burn_sink_balance"] == after["supply"]["burned_total"]
    assert after["supply_equation"]["system_mint_unissued_balance"] == after["supply"]["mint_remaining"]
    assert burn_wallet["points_balance"] == after["funds"]["burn"]["balance"]
    assert after["supply_equation"]["actual_supply_equation_gap_points"] == 0
    assert service.verify_chain()["ok"] is True


def test_rebuild_wallets_from_ledger_starts_its_own_transaction_when_needed(tmp_path):
    service = _service(tmp_path)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=10,
        action_type="test_credit",
        idempotency_key="credit:rebuild-wallets",
    )

    conn = service.get_db()
    try:
        assert conn.in_transaction is False
        rebuild = service._rebuild_wallets_from_ledger(conn)
        assert rebuild["wallets_rebuilt"] >= 1
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_legacy_hard_currency_is_merged_into_single_points_balance(tmp_path):
    service = _service(tmp_path)

    first = service.record_transaction(
        user_id=1,
        currency_type="hard",
        direction="credit",
        amount=5,
        action_type="test_credit",
        idempotency_key="same-key",
    )
    second = service.record_transaction(
        user_id=1,
        currency_type="hard",
        direction="credit",
        amount=5,
        action_type="test_credit",
        idempotency_key="same-key",
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["wallet"]["points_balance"] == 5
    assert second["wallet"]["hard_balance"] == 0
    assert second["ledger"]["currency_type"] == "points"


def test_points_debit_cannot_make_wallet_negative(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="insufficient balance"):
        service.record_transaction(
            user_id=1,
            currency_type="points",
            direction="debit",
            amount=1,
            action_type="test_debit",
        )

    assert service.get_wallet(1)["points_balance"] == 0


def test_spend_points_uses_server_idempotency_when_client_omits_key(tmp_path):
    service = _service(tmp_path)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=100,
        action_type="test_credit",
        idempotency_key="credit:spend",
    )

    first = service.spend_points(user_id=1, item_key="post_cost_standard")
    second = service.spend_points(user_id=1, item_key="post_cost_standard")

    assert first["created"] is True
    assert second["created"] is False
    assert first["settlement_layer"] == "internal_hot_wallet_ledger"
    assert first["ledger"]["direction"] == "debit"
    assert first["charge"]["status"] == "settled"
    assert service.get_wallet(1)["points_balance"] == 99
    assert service.get_wallet(1)["points_frozen"] == 0


def test_pc0_spend_points_debits_internal_hot_wallet_immediately(tmp_path):
    service = _service(tmp_path)
    source = _official_hot_wallet(service, 1)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=150,
        action_type="user_initial_grant",
        idempotency_key="credit:service-fee-batch",
    )

    reserved = service.spend_points(
        user_id=1,
        item_key="comfyui_txt2img_basic",
        source_wallet_address=source["address"],
        request_uuid="service-fee-reserve-small",
        idempotency_key="service-fee-reserve-small",
    )
    wallet_after_reserve = service.get_wallet(1)
    settled = service.spend_points(
        user_id=1,
        item_key="post_pin_24h",
        source_wallet_address=source["address"],
        request_uuid="service-fee-reserve-threshold",
        idempotency_key="service-fee-reserve-threshold",
    )
    final_wallet = service.get_wallet(1)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        charges = conn.execute(
            "SELECT * FROM points_service_fee_charges WHERE user_id=1 ORDER BY id ASC"
        ).fetchall()
        ledgers = conn.execute(
            """
            SELECT direction, action_type, amount, public_metadata_json
            FROM points_ledger
            WHERE action_type LIKE 'service_fee_%' OR action_type LIKE 'service_fee_reserve:%'
            ORDER BY id ASC
            """
        ).fetchall()
        economy_events = conn.execute(
            """
            SELECT transaction_type, source_address, destination_fund_key, destination_address, amount
            FROM points_economy_events
            WHERE transaction_type LIKE 'service_fee_internal_debit:%'
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()

    assert reserved["settlement"]["status"] == "settled"
    assert reserved["settlement_policy"] == "internal_hot_wallet_immediate_debit"
    assert reserved["ledger"]["direction"] == "debit"
    assert reserved["ledger"]["public_metadata"]["settlement_rail"] == "internal_hot_wallet"
    assert reserved["ledger"]["public_metadata"]["network_fee_points"] == 0
    assert reserved["ledger"]["public_metadata"]["service_fee_points"] == 5
    assert wallet_after_reserve["wallet_identity_balances"][source["address"]]["points_balance"] == 145
    assert wallet_after_reserve["wallet_identity_balances"][source["address"]]["points_frozen"] == 0
    assert settled["settlement"]["created"] is True
    assert settled["settlement"]["settled_amount_points"] == 100
    assert settled["ledger"]["public_metadata"]["network_fee_points"] == 0
    assert settled["ledger"]["public_metadata"]["service_fee_points"] == 100
    assert final_wallet["wallet_identity_balances"][source["address"]]["points_balance"] == 45
    assert final_wallet["wallet_identity_balances"][source["address"]]["points_frozen"] == 0
    assert [row["status"] for row in charges] == ["settled", "settled"]
    assert [int(row["amount_points"]) for row in charges] == [5, 100]
    assert [row["direction"] for row in ledgers] == ["debit", "debit"]
    assert all(str(row["action_type"]).startswith("service_fee_internal_debit:") for row in ledgers)
    assert reserved["ledger"]["wallet_flow"]["destination_fund_key"] == "official_treasury"
    assert reserved["ledger"]["wallet_flow"]["destination_wallet_address"] == economy_fund_address(
        service.chain_secret,
        "official_treasury",
    )
    assert len(economy_events) == 2
    assert [row["destination_fund_key"] for row in economy_events] == ["official_treasury", "official_treasury"]
    assert [int(row["amount"]) for row in economy_events] == [5, 100]
    assert service.verify_chain()["ok"] is True


def test_reward_facade_credits_from_promo_fund_and_reconciles_economy(tmp_path):
    service = _service(tmp_path)
    before = service.economy_stats()["economy_layer"]

    reward = service.rc1_facade().grant_reward(
        user_id=2,
        amount=10,
        action_type="valid_bug_report_critical",
        reference_type="bug_report",
        reference_id="bug-test-1",
        idempotency_key="bug-report-reward-test-1",
        reason="valid bug report",
        actor={"id": 3, "username": "root", "role": "super_admin"},
        public_metadata={"bug_report_id": "bug-test-1", "severity": "critical"},
    )

    stats = service.economy_stats()["economy_layer"]
    source_wallet = economy_fund_address(service.chain_secret, "promo_fund")
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        event = conn.execute(
            """
            SELECT *
            FROM points_economy_events
            WHERE idempotency_key=?
            """,
            (f"walletized_ledger:{reward['ledger']['ledger_uuid']}",),
        ).fetchone()
    finally:
        conn.close()

    reward_flow = reward["ledger"]["public_metadata"]["wallet_flow_snapshot"]
    assert reward_flow["source_fund_key"] == "promo_fund"
    assert reward_flow["source_wallet_address"] == source_wallet
    assert reward["ledger"]["public_metadata"]["network_fee_points"] == 0
    assert reward["ledger"]["public_metadata"]["service_fee_points"] == 0
    assert event is not None
    assert event["source_fund_key"] == "promo_fund"
    assert event["destination_fund_key"] is None
    assert int(event["amount"]) == 10
    assert stats["funds"]["promo_fund"]["balance"] == before["funds"]["promo_fund"]["balance"] - 10
    assert stats["supply_equation"]["ledger_vs_economy_external_gap_points"] == 0
    assert stats["supply_equation"]["audit_reconciliation_balanced"] is True
    assert service.verify_chain()["ok"] is True


def test_financial_invariant_report_exposes_reserve_liability_and_bridge_state(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 1, "username": "alice", "role": "user"}
    source = _official_hot_wallet(service, 1)
    recipient = service.get_wallet(2)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=200,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="financial-invariant-fund",
        idempotency_key="financial-invariant-fund",
        actor={"id": 3, "username": "root", "role": "super_admin"},
    )

    withdrawal = service.submit_wallet_transaction(
        actor=actor,
        source_wallet_address=source["address"],
        destination_wallet_address=recipient["deposit_address"],
        amount_points=80,
        fee_points=4,
        request_uuid="financial-invariant-withdrawal",
    )
    _force_request_proved(service, withdrawal["transaction_hash"])
    service.explorer_transaction(withdrawal["transaction_hash"])
    service.economy_stats()

    report = service.financial_invariant_report()

    assert report["ok"] is True
    assert report["model"] == "pc1_canonical_reserve_pc0_wrapped_operational_v1"
    assert report["canonical_reserve"]["active_supply_points"] > 0
    assert report["canonical_reserve"]["canonical_locked_reserve_points"] == report["canonical_reserve"]["active_supply_points"]
    assert report["wrapped_operational_liabilities"]["finalized_total_points"] > 0
    assert report["wrapped_operational_liabilities"]["wrapped_supply_points"] == report["wrapped_operational_liabilities"]["finalized_total_points"]
    assert report["wrapped_operational_liabilities"]["liability_merkle"]["leaf_count"] >= 2
    assert "wrapped_supply_within_canonical_locked_reserve" in {item["name"] for item in report["invariants"]}
    assert report["bridge_reconstruction"]["pc0_out_confirmed_points"] == 80
    assert report["bridge_reconstruction"]["deposit_credited_points"] == 80
    assert report["bridge_reconstruction"]["flow_gap_points"] == 0
    assert report["pending_settlement"]["hot_to_cold_network_fee_points"] == 4
    assert all(item["pass"] is True for item in report["invariants"])
    verify = service.verify_chain()
    assert verify["ok"] is True
    assert verify["financial_ok"] is True
    assert verify["financial_invariants"]["ok"] is True


def test_financial_invariant_report_flags_corrupt_bridge_settlement(tmp_path):
    service = _service(tmp_path)
    wallet = service.get_wallet(2)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO points_chain_bridge_events (
                bridge_uuid, bridge_type, user_id, chain, chain_tx_hash,
                source_address, destination_address, hot_wallet_address, amount_points,
                network_fee_points, confirmations, required_confirmations, risk_status,
                status, internal_ledger_uuid, metadata_json, created_at, updated_at
            ) VALUES (
                'corrupt-bridge-invariant', 'deposit', 2, 'points_chain_sim', 'corrupt-bridge-tx',
                ?, ?, ?, 9, 0, 0, 20, 'review', 'credited', '', '{}',
                '2026-05-25T00:00:00Z', '2026-05-25T00:00:00Z'
            )
            """,
            ("pc1" + ("e" * 48), wallet["deposit_address"], wallet["active_wallet_address"]),
        )
        conn.commit()
    finally:
        conn.close()

    report = service.financial_invariant_report()
    error_types = {item["type"] for item in report["errors"]}

    assert report["ok"] is False
    assert "bridge_deposit_premature_credit" in error_types
    assert "bridge_deposit_missing_internal_credit" in error_types
    assert any(item["name"] == "bridge_settlement_integrity" and item["pass"] is False for item in report["invariants"])


def test_root_can_upsert_service_price_catalog_items(tmp_path):
    service = _service(tmp_path)
    item = service.upsert_catalog_item(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        item_key="comfyui_txt2img_custom",
        item_name="ComfyUI 自定義生圖",
        category="comfyui",
        base_price=9,
        min_price=1,
        max_price=99,
        enabled=True,
        metadata={"note": "root configurable"},
    )

    assert item["item_key"] == "comfyui_txt2img_custom"
    assert item["base_price"] == 9
    catalog = service.list_catalog(include_disabled=True, category="comfyui")
    assert any(row["item_key"] == "comfyui_txt2img_custom" for row in catalog)


def test_cloud_drive_price_catalog_item_requires_storage_metadata(tmp_path):
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="storage_bytes"):
        service.upsert_catalog_item(
            actor={"id": 3, "username": "root", "role": "super_admin"},
            item_key="cloud_storage_custom_missing",
            item_name="缺少容量資料",
            category="cloud_drive",
            base_price=10,
            enabled=True,
            metadata={},
        )


def test_ledger_metadata_has_hard_size_cap(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(ValueError, match="public_metadata is too large"):
        service.record_transaction(
            user_id=1,
            currency_type="points",
            direction="credit",
            amount=1,
            action_type="test_large_metadata",
            public_metadata={"blob": "x" * 5000},
        )


def test_signup_bonus_is_single_currency_and_idempotent(tmp_path):
    service = _service(tmp_path)

    first = service.award_signup_bonus(user_id=1, actor={"id": 1, "username": "alice", "role": "user"})
    second = service.award_signup_bonus(user_id=1, actor={"id": 1, "username": "alice", "role": "user"})

    assert first["created"] is True
    assert second["created"] is False
    assert second["wallet"]["points_balance"] == 100
    assert second["ledger"]["currency_type"] == "points"


def test_birthday_gift_is_once_per_member_per_year(tmp_path):
    service = _service(tmp_path)

    first = service.award_birthday_gift(
        user_id=1,
        birthday_year=2026,
        birthday_date="2026-05-18",
        actor={"id": 1, "username": "alice", "role": "user"},
    )
    second = service.award_birthday_gift(
        user_id=1,
        birthday_year=2026,
        birthday_date="2026-05-18",
        actor={"id": 1, "username": "alice", "role": "user"},
    )
    next_year = service.award_birthday_gift(
        user_id=1,
        birthday_year=2027,
        birthday_date="2027-05-18",
        actor={"id": 1, "username": "alice", "role": "user"},
    )

    assert first["created"] is True
    assert second["created"] is False
    assert next_year["created"] is True
    assert first["ledger"]["action_type"] == "birthday_gift"
    assert first["ledger"]["amount"] == BIRTHDAY_GIFT_POINTS
    assert service.get_wallet(1)["points_balance"] == BIRTHDAY_GIFT_POINTS * 2
    report = service.root_report()
    assert "adjustments" not in report
    assert "birthday_gift" not in [row["action_type"] for row in report["unsealed_transactions"]]
    assert report["verification"]["counts"]["pc0_operational_unsealed_entries"] >= 2


def test_initial_grants_create_genesis_block_once_for_default_accounts(tmp_path):
    service = _service(tmp_path)

    first = service.bootstrap_admin_initial_grants(actor={"username": "system", "role": "system"}, seal_genesis=True)

    assert first["created_count"] == 1
    assert first["created"][0]["username"] == "bob"
    assert first["created"][0]["amount"] == 1000
    assert first["deferred_count"] == 0
    assert first["sealed"] is not None
    assert service.get_wallet(2)["points_balance"] == 1000
    assert service.get_wallet(1)["points_balance"] == 0

    bob = service.award_initial_grants_after_wallet_onboarding(user_id=2, actor={"username": "system", "role": "system"})
    _official_hot_wallet(service, 1)
    alice = service.award_initial_grants_after_wallet_onboarding(user_id=1, actor={"username": "system", "role": "system"})
    second = service.bootstrap_admin_initial_grants(actor={"username": "system", "role": "system"}, seal_genesis=True)

    assert bob["created_count"] == 0
    assert bob["initial_grant"]["granted"] is True
    assert alice["created_count"] == 0
    assert alice["initial_grant"]["required"] is False
    assert second["created_count"] == 0
    assert second["deferred_count"] == 0
    assert service.get_wallet(2)["points_balance"] == 1000
    assert service.get_wallet(1)["points_balance"] == 0
    assert service.get_wallet(3)["points_balance"] == 0
    verification = service.verify_chain()
    assert verification["counts"]["sealed_blocks"] == 0
    assert verification["counts"]["pc0_operational_unsealed_entries"] >= 1
    report = service.root_report()
    assert "adjustments" not in report
    conn = service.get_db()
    try:
        row = conn.execute(
            "SELECT chain_block_id FROM points_ledger WHERE user_id=2 AND action_type='admin_initial_grant'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["chain_block_id"] is None


def test_initial_grant_credits_official_hot_wallet_even_when_cold_wallet_is_primary(tmp_path):
    service = _service(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = _public_jwk(private_key)
    cold_address = address_from_public_key(public_jwk)
    bind_payload = wallet_binding_payload(
        user_id=4,
        wallet_type="self_custody_cold",
        address=cold_address,
        public_key_jwk=public_jwk,
    )

    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute("INSERT INTO users (id, username, role, status) VALUES (4, 'test', 'user', 'active')")
        cold_wallet = bind_self_custody_wallet(
            conn,
            user_id=4,
            wallet_type="self_custody_cold",
            public_key_jwk=public_jwk,
            address=cold_address,
            signature=_signature(private_key, bind_payload),
            backup_confirmed=True,
            label="primary cold",
        )
        assert cold_wallet["is_primary"] is True
        conn.commit()
    finally:
        conn.close()

    grant = service.award_user_initial_grant(user_id=4, actor={"username": "system", "role": "system"})

    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        hot_wallet = conn.execute(
            """
            SELECT * FROM points_wallet_identities
            WHERE user_id=4 AND wallet_type='official_hot'
            LIMIT 1
            """
        ).fetchone()
        ledger_row = conn.execute(
            "SELECT * FROM points_ledger WHERE ledger_uuid=?",
            (grant["ledger"]["ledger_uuid"],),
        ).fetchone()
        flow = service._ledger_wallet_flow_for_read(conn, ledger_row)
        balances = service._wallet_identity_balances_for_user(conn, 4)["balances"]
    finally:
        conn.close()

    assert grant["created"] is True
    assert hot_wallet is not None
    assert flow["destination_wallet_address"] == hot_wallet["address"]
    assert flow["settlement_rail"] == "internal_hot_wallet"
    assert balances[hot_wallet["address"]]["balance"] == USER_INITIAL_POINTS
    assert balances[cold_address]["balance"] == 0


def test_initial_grants_backfill_default_accounts_after_existing_block(tmp_path):
    path = tmp_path / "points-backfill.db"

    def get_db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        from services.platform.db_mode_triggers import register_app_mode_function
        register_app_mode_function(conn, mode_reader=lambda: "production")
        return conn

    conn = get_db()
    conn.execute(
        "CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, role TEXT NOT NULL DEFAULT 'user', status TEXT NOT NULL DEFAULT 'active')"
    )
    conn.execute(
        "INSERT INTO users (username, role, status) VALUES "
        "('alice', 'user', 'active'), "
        "('admin', 'manager', 'active'), "
        "('root', 'super_admin', 'active'), "
        "('test', 'user', 'active')"
    )
    ensure_points_economy_schema(conn)
    conn.commit()
    conn.close()
    service = PointsLedgerService(
        get_db=get_db,
        chain_secret="test-secret",
        backup_dir=tmp_path / "points_chain_backups",
        mode_reader=lambda: "production",
    )

    service.record_transaction(
        user_id=3,
        currency_type="points",
        direction="credit",
        amount=1,
        action_type="preexisting_root_credit",
        idempotency_key="preexisting:block",
    )
    service.seal_block(actor={"username": "root", "role": "super_admin"}, limit=100)
    result = service.bootstrap_admin_initial_grants(actor={"username": "system", "role": "system"}, seal_genesis=True)

    assert result["created_count"] == 2
    assert result["deferred_count"] == 0
    assert {item["username"] for item in result["created"]} == {"admin", "test"}
    admin = service.award_initial_grants_after_wallet_onboarding(user_id=2, actor={"username": "system", "role": "system"})
    test_user = service.award_initial_grants_after_wallet_onboarding(user_id=4, actor={"username": "system", "role": "system"})
    assert admin["created_count"] == 0
    assert test_user["created_count"] == 0
    assert service.get_wallet(2)["points_balance"] == 1000
    assert service.get_wallet(4)["points_balance"] == 100
    assert service.get_wallet(1)["points_balance"] == 0


def test_admin_weekly_salary_is_idempotent_by_week(tmp_path):
    service = _service(tmp_path)

    first = service.award_admin_weekly_salaries(salary_week="2026-W18", actor={"username": "system", "role": "system"})
    second = service.award_admin_weekly_salaries(salary_week="2026-W18", actor={"username": "system", "role": "system"})

    assert first["created_count"] == 1
    assert second["created_count"] == 0
    assert service.get_wallet(2)["points_balance"] == 250
    assert service.get_wallet(3)["points_balance"] == 0


def test_points_ledger_is_append_only(tmp_path):
    service = _service(tmp_path)
    tx = service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=10,
        action_type="test_credit",
    )
    conn = service.get_db()
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM points_ledger WHERE ledger_uuid=?", (tx["ledger"]["ledger_uuid"],))
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("UPDATE points_ledger SET amount=99 WHERE ledger_uuid=?", (tx["ledger"]["ledger_uuid"],))
    finally:
        conn.close()


def test_points_chain_seal_verify_and_proof(tmp_path):
    service = _service(tmp_path)
    tx = _record_pc1_canonical_credit(service, amount=10, action_type="test_pc1_credit")

    sealed = service.seal_block(actor={"id": 3, "username": "root", "role": "super_admin"}, limit=100)
    proof = service.ledger_proof(tx["ledger"]["ledger_uuid"])
    verification = service.verify_chain()

    assert sealed["sealed"] is True
    assert sealed["block"]["ledger_count"] == 1
    assert proof["sealed"] is True
    assert proof["block_number"] == sealed["block"]["block_number"]
    assert proof["ledger_hash"] == tx["ledger"]["ledger_hash"]
    assert verification["ok"] is True
    assert verification["counts"]["unsealed_entries"] == 0
    assert sealed["backup"] is None
    assert sealed["backup_policy"] == "disabled_append_only_chain"
    assert service.list_ledger_backups() == []


def test_pc1_block_seal_excludes_pc0_operational_ledgers(tmp_path):
    service = _service(tmp_path)
    root = {"id": 3, "username": "root", "role": "super_admin"}
    canonical_before = _record_pc1_canonical_credit(service, amount=10, action_type="test_pc1_credit_before")
    source = _official_hot_wallet(service, 1)
    destination = _official_hot_wallet(service, 2)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=80,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-pc0-before-chain-seal",
        idempotency_key="fund-pc0-before-chain-seal",
        public_metadata={
            "settlement_rail": "internal_hot_wallet",
            "chain_required": False,
            "approval_required": False,
            "network_fee_points": 0,
            "service_fee_points": 0,
            "destination_wallet_address": source["address"],
        },
        actor=root,
    )
    internal = service.submit_wallet_transaction(
        actor={"id": 1, "username": "alice", "role": "user"},
        source_wallet_address=source["address"],
        destination_wallet_address=destination["address"],
        amount_points=25,
        request_uuid="pc0-internal-not-pc1-sealed",
    )
    canonical_after = _record_pc1_canonical_credit(service, amount=11, action_type="test_pc1_credit_after_pc0")

    sealed = service.seal_block(actor=root, limit=100)
    verification = service.verify_chain()
    proof_before = service.ledger_proof(canonical_before["ledger"]["ledger_uuid"])
    proof_after = service.ledger_proof(canonical_after["ledger"]["ledger_uuid"])
    proof_internal = service.ledger_proof(internal["transfer_out_ledger"]["ledger_uuid"])

    assert sealed["sealed"] is True
    assert sealed["block"]["ledger_count"] == 2
    assert proof_before["sealed"] is True
    assert proof_after["sealed"] is True
    assert proof_internal["sealed"] is False
    assert verification["ok"] is True
    assert verification["counts"]["unsealed_entries"] == 0
    assert verification["counts"]["pc0_operational_unsealed_entries"] >= 3


def test_financial_invariant_flags_pc0_ledger_sealed_into_pc1_block(tmp_path):
    service = _service(tmp_path)
    root = {"id": 3, "username": "root", "role": "super_admin"}
    _record_pc1_canonical_credit(service, amount=10, action_type="test_pc1_credit_for_boundary")
    sealed = service.seal_block(actor=root, limit=100)
    source = _official_hot_wallet(service, 1)
    destination = _official_hot_wallet(service, 2)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=80,
        action_type="admin_adjust_credit",
        reference_type="test",
        reference_id="fund-pc0-boundary-corrupt",
        idempotency_key="fund-pc0-boundary-corrupt",
        public_metadata={
            "settlement_rail": "internal_hot_wallet",
            "chain_required": False,
            "approval_required": False,
            "network_fee_points": 0,
            "service_fee_points": 0,
            "destination_wallet_address": source["address"],
        },
        actor=root,
    )
    internal = service.submit_wallet_transaction(
        actor={"id": 1, "username": "alice", "role": "user"},
        source_wallet_address=source["address"],
        destination_wallet_address=destination["address"],
        amount_points=25,
        request_uuid="pc0-internal-boundary-corruption",
    )

    conn = service.get_db()
    try:
        conn.execute(
            "UPDATE points_ledger SET chain_block_id=? WHERE ledger_uuid=?",
            (sealed["block"]["id"], internal["transfer_out_ledger"]["ledger_uuid"]),
        )
        conn.commit()
    finally:
        conn.close()

    report = service.financial_invariant_report()
    error_types = {item["type"] for item in report["errors"]}

    assert report["ok"] is False
    assert "pc0_operational_ledger_sealed_into_pc1_block" in error_types
    boundary = report["ledger_boundary"]
    assert boundary["ok"] is False
    assert boundary["counts"]["sealed_pc0_operational_ledgers"] == 1
    assert any(
        item["name"] == "pc0_operational_ledgers_not_sealed_into_pc1_blocks" and item["pass"] is False
        for item in report["invariants"]
    )


def test_deposit_bridge_credit_is_not_sealed_into_pc1_block(tmp_path):
    service = _service(tmp_path)
    root = {"id": 3, "username": "root", "role": "super_admin"}
    wallet = service.get_wallet(2)
    result = service.confirm_deposit_to_hot_wallet(
        actor=root,
        user_id=2,
        source_address="pc1" + ("d" * 48),
        destination_address=wallet["deposit_address"],
        amount_points=70,
        chain_tx_hash="deposit-chain-tx-not-pc1-sealed",
    )

    sealed = service.seal_block(actor=root, limit=100)
    proof = service.ledger_proof(result["ledger"]["ledger_uuid"])
    verification = service.verify_chain()

    assert sealed["sealed"] is False
    assert proof["sealed"] is False
    assert verification["ok"] is True
    assert verification["counts"]["unsealed_entries"] == 0
    assert verification["counts"]["pc0_operational_unsealed_entries"] >= 1


def test_points_chain_verify_identifies_tampered_ledger(tmp_path):
    service = _service(tmp_path)
    tx = service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=10,
        action_type="test_credit",
    )
    conn = service.get_db()
    try:
        conn.execute("DROP TRIGGER trg_points_ledger_core_immutable")
        conn.execute("UPDATE points_ledger SET amount=99 WHERE ledger_uuid=?", (tx["ledger"]["ledger_uuid"],))
        conn.commit()
    finally:
        conn.close()

    verification = service.verify_chain()
    assert verification["ok"] is False
    error = next(row for row in verification["errors"] if row["type"] == "ledger_hash")
    assert error["ledger_uuid"] == tx["ledger"]["ledger_uuid"]
    assert error["ledger"]["amount"] == 99
    assert error["expected_ledger_hash"]
    assert error["actual_ledger_hash"] == tx["ledger"]["ledger_hash"]

    report = service.root_report()
    flagged = next(row for row in report["high_risk_ledger"] if row["ledger_uuid"] == tx["ledger"]["ledger_uuid"])
    assert flagged["verification_status"] == "tampered"
    assert flagged["verification_errors"][0]["type"] == "ledger_hash"
    recovery = service.safe_mode_status()
    assert recovery["safe_mode"] is True
    assert recovery["forensic_bundle_id"]
    assert recovery["restore_plan"]["auto_apply"] is False


def test_points_chain_forensic_bundle_does_not_inline_full_ledger():
    source = (ROOT / "services" / "points_chain" / "backup_recovery.py").read_text(encoding="utf-8")

    assert '"full_ledger_inline": False' in source
    assert '"current_ledger": [dict(row) for row in conn.execute("SELECT * FROM points_ledger ORDER BY id ASC").fetchall()]' not in source


def test_points_chain_block_and_signature_tables_are_append_only(tmp_path):
    service = _service(tmp_path)
    _record_pc1_canonical_credit(service, amount=10, action_type="test_pc1_append_only")
    sealed = service.seal_block(actor={"id": 3, "username": "root", "role": "super_admin"}, limit=100)
    block_id = sealed["block"]["id"]

    conn = service.get_db()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="points chain blocks are append-only"):
            conn.execute("UPDATE points_chain_blocks SET merkle_root='forged' WHERE id=?", (block_id,))
        with pytest.raises(sqlite3.IntegrityError, match="points chain blocks are append-only"):
            conn.execute("DELETE FROM points_chain_blocks WHERE id=?", (block_id,))
        with pytest.raises(sqlite3.IntegrityError, match="points chain block signatures are append-only"):
            conn.execute("UPDATE points_chain_block_signatures SET signature='forged' WHERE block_id=?", (block_id,))
        with pytest.raises(sqlite3.IntegrityError, match="points chain block signatures are append-only"):
            conn.execute("DELETE FROM points_chain_block_signatures WHERE block_id=?", (block_id,))
    finally:
        conn.rollback()
        conn.close()


def test_points_chain_verify_detects_forged_sealed_transaction_hash_recompute(tmp_path):
    service = _service(tmp_path)
    tx = _record_pc1_canonical_credit(service, amount=10, action_type="test_pc1_forged_hash")
    service.seal_block(actor={"id": 3, "username": "root", "role": "super_admin"}, limit=100)

    conn = service.get_db()
    try:
        conn.execute("DROP TRIGGER trg_points_ledger_core_immutable")
        conn.execute("UPDATE points_ledger SET amount=99 WHERE ledger_uuid=?", (tx["ledger"]["ledger_uuid"],))
        forged = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (tx["ledger"]["ledger_uuid"],)).fetchone()
        conn.execute(
            "UPDATE points_ledger SET ledger_hash=? WHERE ledger_uuid=?",
            (compute_ledger_hash(forged), tx["ledger"]["ledger_uuid"]),
        )
        conn.commit()
    finally:
        conn.close()

    verification = service.verify_chain()
    assert verification["ok"] is False
    assert any(error["type"] == "block_merkle_root" for error in verification["errors"])
    assert service.safe_mode_status()["safe_mode"] is True
    with pytest.raises(ValueError, match="safe mode"):
        service.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1, action_type="blocked")


def test_points_chain_verify_detects_forged_block_rehash_without_node_signature(tmp_path):
    service = _service(tmp_path)
    tx = _record_pc1_canonical_credit(service, amount=10, action_type="test_pc1_forged_block")
    service.seal_block(actor={"id": 3, "username": "root", "role": "super_admin"}, limit=100)

    conn = service.get_db()
    try:
        conn.execute("DROP TRIGGER trg_points_ledger_core_immutable")
        conn.execute("DROP TRIGGER trg_points_chain_blocks_no_update")
        conn.execute("UPDATE points_ledger SET amount=99 WHERE ledger_uuid=?", (tx["ledger"]["ledger_uuid"],))
        forged = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (tx["ledger"]["ledger_uuid"],)).fetchone()
        conn.execute(
            "UPDATE points_ledger SET ledger_hash=? WHERE ledger_uuid=?",
            (compute_ledger_hash(forged), tx["ledger"]["ledger_uuid"]),
        )
        block = conn.execute("SELECT * FROM points_chain_blocks ORDER BY block_number ASC LIMIT 1").fetchone()
        hashes = [
            row["ledger_hash"]
            for row in conn.execute(
                "SELECT ledger_hash FROM points_ledger WHERE chain_block_id=? ORDER BY id ASC",
                (block["id"],),
            ).fetchall()
        ]
        forged_merkle = merkle_root(hashes)
        conn.execute("UPDATE points_chain_blocks SET merkle_root=? WHERE id=?", (forged_merkle, block["id"]))
        forged_block = conn.execute("SELECT * FROM points_chain_blocks WHERE id=?", (block["id"],)).fetchone()
        conn.execute(
            "UPDATE points_chain_blocks SET block_hash=? WHERE id=?",
            (compute_block_hash(forged_block), block["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    verification = service.verify_chain()
    assert verification["ok"] is False
    assert any(error["type"] == "block_signature_invalid" for error in verification["errors"])
    assert service.safe_mode_status()["safe_mode"] is True


def test_points_chain_verify_does_not_auto_resign_missing_block_signature(tmp_path):
    service = _service(tmp_path)
    _record_pc1_canonical_credit(service, amount=10, action_type="test_pc1_missing_signature")
    service.seal_block(actor={"id": 3, "username": "root", "role": "super_admin"}, limit=100)

    conn = service.get_db()
    try:
        conn.execute("DROP TRIGGER trg_points_chain_block_signatures_no_delete")
        conn.execute("DELETE FROM points_chain_block_signatures")
        conn.commit()
    finally:
        conn.close()

    verification = service.verify_chain()
    assert verification["ok"] is False
    assert any(error["type"] == "block_signature_missing" for error in verification["errors"])
    conn = service.get_db()
    try:
        count = conn.execute("SELECT COUNT(*) FROM points_chain_block_signatures").fetchone()[0]
    finally:
        conn.close()
    assert count == 0


def test_points_chain_safe_mode_blocks_writes_and_backup_restore_is_disabled(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 3, "username": "root", "role": "super_admin"}
    tx = _record_pc1_canonical_credit(service, amount=30, action_type="test_pc1_safe_mode")
    sealed = service.seal_block(actor=actor, limit=100)

    conn = service.get_db()
    try:
        conn.execute("DROP TRIGGER trg_points_ledger_core_immutable")
        conn.execute("UPDATE points_ledger SET amount=99 WHERE ledger_uuid=?", (tx["ledger"]["ledger_uuid"],))
        conn.execute("UPDATE points_wallets SET soft_balance=999 WHERE user_id=1")
        conn.commit()
    finally:
        conn.close()

    verification = service.verify_chain()
    assert verification["ok"] is False
    assert service.safe_mode_status()["safe_mode"] is True

    with pytest.raises(ValueError, match="safe mode"):
        service.record_transaction(user_id=1, currency_type="points", direction="credit", amount=1, action_type="blocked")
    with pytest.raises(ValueError, match="safe mode"):
        service.seal_block(actor=actor, limit=100)

    assert sealed["backup"] is None
    assert service.list_ledger_backups() == []
    recovery = service.safe_mode_status()
    assert recovery["safe_mode"] is True
    assert recovery["restore_plan"]["mode"] == "branch_governance_recovery"
    assert recovery["restore_plan"]["backup_restore_disabled"] is True
    with pytest.raises(PermissionError, match="backup restore is disabled"):
        service.restore_from_backup(actor=actor, backup_id="legacy", confirm="RESTORE POINTSCHAIN")


def test_points_chain_repairs_wallet_cache_tamper_without_entering_restore_mode(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 3, "username": "root", "role": "super_admin"}
    _record_pc1_canonical_credit(service, amount=45, action_type="test_pc1_wallet_cache_repair")
    sealed = service.seal_block(actor=actor, limit=100)

    conn = service.get_db()
    try:
        conn.execute("UPDATE points_wallets SET soft_balance=999, total_soft_earned=999 WHERE user_id=1")
        conn.commit()
    finally:
        conn.close()

    verification = service.verify_chain()
    assert verification["ok"] is True
    assert verification["repairs"][0]["type"] == "wallet_identity_cache_rebuilt"
    assert service.safe_mode_status()["safe_mode"] is False

    assert sealed["backup"] is None
    with pytest.raises(PermissionError, match="backup restore is disabled"):
        service.restore_from_backup(actor=actor, backup_id="legacy", confirm="RESTORE POINTSCHAIN")
    assert service.get_wallet(1)["points_balance"] == 45


def test_verify_chain_uses_wallet_identity_balances_for_walletized_user(tmp_path):
    service = _service(tmp_path)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=100,
        action_type="user_initial_grant",
        reference_type="legacy_bootstrap",
        reference_id="before-wallet",
        idempotency_key="legacy-before-wallet",
        reason="legacy initial grant before wallet identity",
    )
    wallet = _official_hot_wallet(service, 1)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=100,
        action_type="new_user_signup_bonus",
        reference_type="wallet_signup",
        reference_id="after-wallet",
        idempotency_key="wallet-after-wallet",
        reason="walletized signup bonus",
    )

    payload = service.get_wallet(1)
    assert payload["wallet_identity_balances"][wallet["address"]]["points_balance"] == 200
    assert payload["account_points_balance"] == 200
    assert service.verify_chain()["ok"] is True

    conn = service.get_db()
    try:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        conn.execute(
            """
            INSERT OR REPLACE INTO points_chain_recovery_state
            (id, safe_mode, reason, verification_json, created_at, updated_at)
            VALUES (1, 1, 'legacy_identity_false_positive', '{}', ?, ?)
            """,
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()
    verification = service.verify_chain()
    assert verification["ok"] is True
    assert verification["safe_mode"]["safe_mode"] is False
    assert service.safe_mode_status()["safe_mode"] is False


def test_tampered_ledger_cannot_be_rolled_back_from_dirty_row(tmp_path):
    service = _service(tmp_path)
    tx = service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=10,
        action_type="test_credit",
    )
    conn = service.get_db()
    try:
        conn.execute("DROP TRIGGER trg_points_ledger_core_immutable")
        conn.execute("UPDATE points_ledger SET amount=99 WHERE ledger_uuid=?", (tx["ledger"]["ledger_uuid"],))
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="tampered"):
        service.compensate_ledger(
            actor={"id": 3, "username": "root", "role": "super_admin"},
            ledger_uuid=tx["ledger"]["ledger_uuid"],
            reason="dirty compensation should be blocked",
        )


def test_points_chain_seal_adds_local_signature_and_root_report(tmp_path):
    service = _service(tmp_path)
    _record_pc1_canonical_credit(service, amount=10, action_type="test_pc1_root_report")

    sealed = service.seal_block(actor={"id": 3, "username": "root", "role": "super_admin"}, limit=100)
    destination = _official_hot_wallet(service, 1)
    _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=destination["address"],
        amount=7,
        request_uuid="root-report-official-grant",
    )
    report = service.root_report()

    assert sealed["sealed"] is True
    assert report["verification"]["ok"] is True
    assert report["blocks"][0]["signature_algorithm"] == "hmac-sha256"
    assert report["ledger_backups"] == []
    assert report["scheduled_backup"]["disabled"] is True
    assert report["audit_logs"][0]["event_type"] in {
        "POINTS_BLOCK_SEALED",
        "POINTS_CHAIN_WALLET_CACHE_REBUILT",
        "POINTS_CHAIN_GOVERNANCE_PROPOSAL_EXECUTED",
        "LEDGER_APPEND",
        "OFFICIAL_WALLET_GRANT",
    }
    assert report["block_schedule"]["mode"] == "hybrid"
    assert report["block_schedule"]["ledger_threshold"] == 30
    assert report["block_schedule"]["max_interval_minutes"] == 24 * 60
    assert report["block_schedule"]["unsealed_entries"] == 0
    assert report["verification"]["counts"]["pc0_operational_unsealed_entries"] >= 1
    assert "adjustments" not in report
    assert all(item["action_type"] != "official_wallet_grant" for item in report["unsealed_transactions"])
    report_json = json.dumps(report, ensure_ascii=False)
    assert '"target_username"' not in report_json
    assert '"actor_username"' not in report_json
    assert '"user_id"' not in report_json
    assert '"soft"' not in report_json
    assert '"hard"' not in report_json


def test_root_report_sanitizes_legacy_currency_audit_text(tmp_path):
    service = _service(tmp_path)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=10,
        action_type="test_credit",
    )
    conn = service.get_db()
    try:
        conn.execute(
            """
            INSERT INTO points_chain_audit_logs (
                event_type, severity, actor_user_id, actor_role, target_user_id,
                related_ledger_id, related_block_id, message, metadata_json, created_at
            ) VALUES ('LEGACY_AUDIT', 'info', NULL, NULL, 1, NULL, NULL, ?, ?, '2026-04-29T00:00:00Z')
            """,
            (
                "credit 10 soft for user 1",
                json.dumps({"currency_type": "soft", "note": "legacy hard reward"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    report = service.root_report()
    legacy = next(row for row in report["audit_logs"] if row["event_type"] == "LEGACY_AUDIT")

    assert legacy["message"] == "credit 10 points for user 1"
    assert legacy["metadata"]["currency_type"] == "points"
    assert legacy["metadata"]["note"] == "legacy points reward"
    assert '"soft"' not in json.dumps(report, ensure_ascii=False)
    assert '"hard"' not in json.dumps(report, ensure_ascii=False)


def test_root_rollback_is_disabled_and_compensation_is_append_only(tmp_path):
    service = _service(tmp_path)
    tx = service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=10,
        action_type="test_credit",
    )

    with pytest.raises(PermissionError, match="rollback is disabled"):
        service.rollback_ledger(
            actor={"id": 3, "username": "root", "role": "super_admin"},
            ledger_uuid=tx["ledger"]["ledger_uuid"],
            reason="emergency correction",
        )

    compensation = service.compensate_ledger(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        ledger_uuid=tx["ledger"]["ledger_uuid"],
        reason="emergency correction",
    )

    assert compensation["compensation_ledger"]["direction"] == "reverse"
    assert compensation["compensation_ledger"]["reference_id"] == tx["ledger"]["ledger_uuid"]
    assert compensation["wallet"]["points_balance"] == 0
    assert service.verify_chain()["ok"] is True
    audit_events = [row["event_type"] for row in service.list_chain_audit_logs(limit=10)]
    assert "LEDGER_COMPENSATION" in audit_events

    duplicate = service.compensate_ledger(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        ledger_uuid=tx["ledger"]["ledger_uuid"],
        reason="duplicate correction",
    )
    assert duplicate["created"] is False


def test_direct_wallet_sanction_is_disabled(tmp_path):
    service = _service(tmp_path)
    root_actor = {"id": 3, "username": "root", "role": "super_admin"}
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=20,
        action_type="test_credit",
    )

    with pytest.raises(PermissionError, match="direct wallet sanctions are disabled"):
        service.sanction_wallet(
            actor=root_actor,
            user_id=1,
            wallet_status="frozen",
            risk_level="high",
            reason="abuse investigation",
            freeze_amount=7,
        )
    assert service.get_wallet(1)["wallet_status"] == "active"
    assert service.get_wallet(1)["points_balance"] == 20


def test_points_chain_auto_seals_when_hybrid_count_threshold_is_met(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 3, "username": "root", "role": "super_admin"}
    for idx in range(9):
        _record_pc1_canonical_credit(service, amount=1, action_type=f"test_pc1_credit_{idx}")

    pending = service.seal_due_block(actor=actor, ledger_threshold=10)
    assert pending["sealed"] is False
    assert pending["schedule"]["unsealed_entries"] == 9
    assert pending["schedule"]["entries_remaining"] == 1

    _record_pc1_canonical_credit(service, amount=1, action_type="test_pc1_credit_9")
    sealed = service.seal_due_block(actor=actor, ledger_threshold=10)

    assert sealed["sealed"] is True
    assert sealed["block"]["ledger_count"] == 10
    assert service.verify_chain()["counts"]["unsealed_entries"] == 0


def test_points_chain_not_due_schedule_avoids_full_verification(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 3, "username": "root", "role": "super_admin"}
    for idx in range(3):
        _record_pc1_canonical_credit(service, amount=1, action_type=f"schedule_pc1_light_check_{idx}")

    calls = 0

    def counted_verify_chain():
        nonlocal calls
        calls += 1
        raise AssertionError("seal_due_block should not verify the whole chain before the schedule is due")

    service.verify_chain = counted_verify_chain
    pending = service.seal_due_block(actor=actor, ledger_threshold=10)

    assert pending["sealed"] is False
    assert pending["schedule"]["unsealed_entries"] == 3
    assert calls == 0


def test_points_chain_due_seal_uses_bounded_verification(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 3, "username": "root", "role": "super_admin"}
    _record_pc1_canonical_credit(service, amount=1, action_type="schedule_pc1_bounded_check")

    def forbidden_full_verify(*args, **kwargs):
        raise AssertionError("seal_due_block should use bounded verification before sealing")

    service.verify_chain = forbidden_full_verify
    sealed = service.seal_due_block(actor=actor, ledger_threshold=1)

    assert sealed["sealed"] is True
    assert sealed["schedule"]["verification_bounded"] is True
    assert sealed["schedule"]["verification_mode"] == "bounded_recent_snapshot"


def test_points_chain_force_seal_uses_bounded_verification(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 3, "username": "root", "role": "super_admin"}
    _record_pc1_canonical_credit(service, amount=1, action_type="force_pc1_bounded_check")

    def forbidden_full_verify(*args, **kwargs):
        raise AssertionError("force_seal_block should use bounded verification before sealing")

    service.verify_chain = forbidden_full_verify
    sealed = service.force_seal_block(actor=actor, reason="test_force_bounded")

    assert sealed["sealed"] is True
    assert sealed["forced"] is True
    assert sealed["pre_seal_verification"]["bounded"] is True


def test_points_chain_root_report_reuses_single_verification(tmp_path):
    service = _service(tmp_path)
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=10,
        action_type="root_report_single_verify",
    )
    original_verify_chain = service.verify_chain
    calls = 0

    def counted_verify_chain(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_verify_chain(*args, **kwargs)

    service.verify_chain = counted_verify_chain
    report = service.root_report()

    assert calls == 1
    assert report["verification"]["ok"] is True
    assert report["stats"]["chain"]["counts"] == report["verification"]["counts"]
    assert report["block_schedule"]["chain_ok"] is True


def test_points_chain_runtime_reset_clears_active_ledger_and_leaves_reset_audit(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 3, "username": "root", "role": "super_admin"}
    _record_pc1_canonical_credit(service, amount=10, action_type="test_pc1_reset")
    sealed = service.seal_block(actor=actor)
    assert sealed["sealed"] is True
    assert sealed["backup"] is None
    assert service.list_ledger_backups() == []
    assert service.verify_chain()["counts"]["ledger_entries"] == 1

    reset = service.reset_runtime_chain(actor=actor, reason="server reset", pre_reset_snapshot_id="snap_test")

    assert reset["ok"] is True
    verification = service.verify_chain()
    assert verification["ok"] is True
    assert verification["counts"]["ledger_entries"] == 0
    assert verification["counts"]["sealed_blocks"] == 0
    assert service.list_ledger_backups() == []
    assert service.get_wallet(1)["points_balance"] == 0
    audit_events = service.list_chain_audit_logs(limit=5)
    assert audit_events[0]["event_type"] == "POINTS_CHAIN_RESET"


def test_manual_admin_adjust_is_disabled(tmp_path):
    service = _service(tmp_path)

    with pytest.raises(PermissionError, match="manual points adjustment is disabled"):
        service.admin_adjust(
            actor={"id": 3, "username": "root", "role": "super_admin"},
            user_id=1,
            currency_type="points",
            direction="credit",
            amount=5,
            reason="manual correction",
        )


def test_official_wallet_grant_is_idempotent_for_request_uuid(tmp_path):
    service = _service(tmp_path)
    actor = {"id": 3, "username": "root", "role": "super_admin"}
    destination = _official_hot_wallet(service, 1)

    first, proposal_uuid = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=destination["address"],
        amount=5,
        request_uuid="official-grant-click-1",
    )
    second = service.official_wallet_grant(
        actor=actor,
        destination_wallet_address=destination["address"],
        amount=5,
        reason="governance official transfer",
        request_uuid=f"governance:{proposal_uuid}:official_transfer",
        governance_proposal_uuid=proposal_uuid,
    )

    assert first["created"] is True
    assert second["created"] is False
    assert first["transaction"]["status"] == "confirmed"
    assert second["transaction"]["status"] == "confirmed"
    assert service.get_wallet(1)["points_balance"] == 5
    proved = service.explorer_transaction(first["transaction_hash"])["transaction"]

    assert proved["status"] == "confirmed"
    grants = [
        row for row in service.list_ledger(user_id=1, include_user_id=True)
        if row["action_type"] == "official_wallet_grant"
    ]
    assert len(grants) == 1


def test_official_wallet_grant_requires_root_but_allows_unowned_wallet_address(tmp_path):
    service = _service(tmp_path)
    destination = _official_hot_wallet(service, 1)
    unowned_address = "pc1" + "9" * 48

    with pytest.raises(PermissionError, match="requires executed governance proposal"):
        service.official_wallet_grant(
            actor={"id": 2, "username": "bob", "role": "manager"},
            destination_wallet_address=destination["address"],
            amount=1,
            reason="must fail",
            request_uuid="non-root-official-grant",
        )
    result, _proposal_uuid = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=unowned_address,
        amount=1,
        request_uuid="unknown-official-grant",
    )
    assert result["created"] is True
    assert result["destination_unowned"] is True
    assert result["transaction"]["wallet_flow"]["destination_unowned"] is True
    assert result["transaction"]["status"] == "pending"
    assert service.explorer_wallet(unowned_address)["wallet"]["points_balance"] == 0

    _force_request_proved(service, result["transaction_hash"])
    proved = service.explorer_transaction(result["transaction_hash"])["transaction"]

    assert proved["status"] == "confirmed"
    assert service.explorer_wallet(unowned_address)["wallet"]["points_balance"] == 1


def test_spend_points_uses_selected_source_wallet(tmp_path):
    service = _service(tmp_path)
    secondary_key = ec.generate_private_key(ec.SECP256R1())
    secondary_public_jwk = _public_jwk(secondary_key)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        primary = create_official_hot_wallet(conn, user_id=1, chain_secret=service.chain_secret, label="primary hot")["address"]
        secondary = address_from_public_key(secondary_public_jwk)
        bind_payload = wallet_binding_payload(
            user_id=1,
            wallet_type="self_custody_cold",
            address=secondary,
            public_key_jwk=secondary_public_jwk,
        )
        secondary_wallet = bind_self_custody_wallet(
            conn,
            user_id=1,
            wallet_type="self_custody_cold",
            public_key_jwk=secondary_public_jwk,
            address=secondary,
            signature=_signature(secondary_key, bind_payload),
            label="secondary cold",
            backup_confirmed=True,
        )
        conn.commit()
    finally:
        conn.close()
    service.upsert_catalog_item(
        actor={"id": 3, "username": "root", "role": "super_admin"},
        item_key="unit_spend_item",
        item_name="Unit Spend Item",
        category="test",
        base_price=3,
        enabled=True,
    )
    grant, _proposal_uuid = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=secondary,
        amount=10,
        request_uuid="fund-secondary-wallet",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    spend_request_uuid = "selected-source-service-fee"
    spend_signature_payload = wallet_service_fee_payload(
        user_id=1,
        source_wallet_address=secondary,
        item_key="unit_spend_item",
        quantity=1,
        amount_points=3,
        request_uuid=spend_request_uuid,
        reference_type="price_catalog",
        reference_id="unit_spend_item",
        chain_branch="main",
        signer_key_id=secondary_wallet["public_key_hash"],
    )
    with pytest.raises(ValueError, match="cold wallet direct service payment is disabled"):
        service.spend_points(
            user_id=1,
            item_key="unit_spend_item",
            quantity=1,
            source_wallet_address=secondary,
            request_uuid=spend_request_uuid,
            idempotency_key=spend_request_uuid,
            signature=_signature(secondary_key, spend_signature_payload),
            actor={"id": 1, "username": "alice", "role": "user"},
        )
    wallet_after = service.get_wallet(1)
    assert wallet_after["wallet_identity_balances"][secondary]["points_balance"] == 10
    assert wallet_after["wallet_identity_balances"][secondary]["points_frozen"] == 0
    assert wallet_after["wallet_identity_balances"][primary]["points_balance"] == 0

    with pytest.raises(ValueError, match="insufficient balance"):
        service.spend_points(
            user_id=1,
            item_key="unit_spend_item",
            quantity=1,
            source_wallet_address=primary,
            actor={"id": 1, "username": "alice", "role": "user"},
        )


def test_points_chain_ledger_backups_are_disabled(tmp_path):
    service = _service(tmp_path)
    manual = service.create_ledger_backup(reason="unit", kind="manual")
    scheduled = service.create_scheduled_backup_if_due()
    assert manual["disabled"] is True
    assert manual["created"] is False
    assert scheduled["disabled"] is True
    assert scheduled["created"] is False
    assert service.list_ledger_backups() == []
