import base64
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils

from services.points_chain import (
    PointsLedgerService,
    address_dispute_payload,
    address_from_public_key,
    bind_self_custody_wallet,
    create_official_hot_wallet,
    ensure_points_economy_schema,
    wallet_binding_payload,
    wallet_service_fee_payload,
    wallet_transaction_payload,
)
from services.points_chain.schema import canonical_json, sha256_text
from services.points_chain.economy_layer import append_economy_event, economy_fund_address, economy_layer_report
import services.points_chain.service as points_service_module
from services.trading.trading_engine import TradingEngineService, ensure_trading_schema


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
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_transfer_requests SET created_at='2026-01-01T00:00:00Z' WHERE tx_group_hash=?",
            (tx_hash,),
        )
        conn.commit()
    finally:
        conn.close()


def _service(tmp_path, *, user_count=10, mode="dev_ready"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "governance.db"

    def get_db():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        from services.platform.db_mode_triggers import register_app_mode_function

        register_app_mode_function(conn, mode_reader=lambda: mode)
        return conn

    conn = get_db()
    conn.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            role TEXT NOT NULL DEFAULT 'user',
            status TEXT NOT NULL DEFAULT 'active',
            member_level TEXT NOT NULL DEFAULT 'normal',
            base_level TEXT NOT NULL DEFAULT 'normal',
            effective_level TEXT NOT NULL DEFAULT 'normal'
        )
        """
    )
    conn.execute("INSERT INTO users (username, role, status, member_level, base_level, effective_level) VALUES ('root', 'super_admin', 'active', 'vip', 'vip', 'vip')")
    for index in range(2, user_count + 1):
        role = "manager" if index == 2 else "user"
        level = "trusted" if index == 3 else "normal"
        conn.execute(
            "INSERT INTO users (username, role, status, member_level, base_level, effective_level) VALUES (?, ?, 'active', ?, ?, ?)",
            (f"user{index}", role, level, level, level),
        )
    ensure_points_economy_schema(conn)
    conn.commit()
    conn.close()
    return PointsLedgerService(
        get_db=get_db,
        chain_secret="governance-secret",
        backup_dir=tmp_path / "backups",
        mode_reader=lambda: mode,
    )


def _actor(user_id, username, role="user", **extra):
    actor = {"id": user_id, "username": username, "role": role}
    actor.update(extra)
    return actor


def test_transaction_dispute_schema_add_column_race_is_idempotent(tmp_path, monkeypatch):
    service = _service(tmp_path)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        service._ensure_transaction_dispute_schema(conn)
        conn.commit()
        real_table_columns = points_service_module.table_columns
        db_path = points_service_module._connection_path(conn)
        service._transaction_dispute_schema_ready_paths.discard(db_path)

        def stale_table_columns(target_conn, table_name):
            cols = set(real_table_columns(target_conn, table_name))
            if table_name == "points_chain_transaction_disputes":
                cols.discard("reply_nonce")
            return cols

        monkeypatch.setattr(points_service_module, "table_columns", stale_table_columns)
        service._ensure_transaction_dispute_schema(conn)
        conn.commit()
        assert "reply_nonce" in real_table_columns(conn, "points_chain_transaction_disputes")
    finally:
        conn.close()


def _rate_field(prefix):
    return f"{prefix}_b" + "ps"


def _official_hot_wallet(service, user_id):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        wallet = create_official_hot_wallet(conn, user_id=user_id, chain_secret=service.chain_secret)
        conn.commit()
        return wallet
    finally:
        conn.close()


def _trading_service(service):
    return TradingEngineService(
        get_db=service.get_db,
        points_service=service,
        live_price_provider=lambda symbol: {
            "BTC/POINTS": 100000,
            "ETH/POINTS": 5000,
            "XRP/POINTS": 3,
            "BNB/POINTS": 700,
            "PAXG/POINTS": 3300,
        }.get(symbol, 1000),
    )


def _self_custody_wallet(service, user_id):
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
        return wallet, private_key
    finally:
        conn.close()


def _signed_wallet_transfer(service, *, actor, source_wallet, source_key, destination_wallet_address="", destination_address="", amount=0, fee=0, request_uuid="", memo=""):
    destination = destination_wallet_address or destination_address
    branch = service.explorer_wallet(source_wallet["address"])["wallet"]["chain_branch"]
    payload = wallet_transaction_payload(
        user_id=actor["id"],
        source_wallet_address=source_wallet["address"],
        destination_wallet_address=destination,
        amount_points=amount,
        fee_points=fee,
        request_uuid=request_uuid,
        memo=memo,
        chain_branch=branch,
        signer_key_id=source_wallet["public_key_hash"],
    )
    return service.submit_wallet_transaction(
        actor=actor,
        source_wallet_address=source_wallet["address"],
        destination_wallet_address=destination,
        amount_points=amount,
        fee_points=fee,
        request_uuid=request_uuid,
        memo=memo,
        signature=_signature(source_key, payload),
    )


def _address_signed_dispute(service, *, actor, tx_hash, from_wallet, from_key, to_wallet_address, amount, statement, evidence=None, nonce="dispute-open-nonce"):
    tx = service.explorer_transaction(tx_hash)["transaction"]
    statement_hash = sha256_text(statement)
    evidence_items = [str(item or "").strip()[:240] for item in (evidence or []) if str(item or "").strip()]
    evidence_hash = sha256_text(canonical_json(evidence_items))
    runtime_mode = service._address_dispute_runtime_mode()
    payload = address_dispute_payload(
        tx_hash=tx_hash,
        from_wallet_address=from_wallet["address"],
        to_wallet_address=to_wallet_address,
        amount_points=amount,
        statement_hash=statement_hash,
        evidence_hash=evidence_hash,
        nonce=nonce,
        chain_branch=tx["chain_branch"],
        purpose="address_dispute_open",
        runtime_mode=runtime_mode,
    )
    return service.create_transaction_dispute(
        actor=actor,
        tx_hash=tx_hash,
        statement=statement,
        victim_wallet_address=from_wallet["address"],
        claimed_amount_points=amount,
        loss_cause="private_key_leak",
        evidence=evidence_items,
        public_key_jwk=_public_jwk(from_key),
        signature=_signature(from_key, payload),
        signature_nonce=nonce,
        from_wallet_address=from_wallet["address"],
        to_wallet_address=to_wallet_address,
        chain_branch=tx["chain_branch"],
    )


def _address_signed_dispute_reply(service, *, actor, dispute, to_key, statement, evidence=None, nonce="dispute-reply-nonce"):
    evidence_items = [str(item or "").strip()[:240] for item in (evidence or []) if str(item or "").strip()]
    runtime_mode = str(dispute.get("signature_runtime_mode") or service._address_dispute_runtime_mode())
    payload = address_dispute_payload(
        tx_hash=dispute["tx_hash"],
        from_wallet_address=dispute["from_wallet_address"],
        to_wallet_address=dispute["to_wallet_address"],
        amount_points=dispute["claimed_amount_points"],
        statement_hash=sha256_text(statement),
        evidence_hash=sha256_text(canonical_json(evidence_items)),
        nonce=nonce,
        chain_branch=dispute["chain_branch"],
        purpose="address_dispute_reply",
        runtime_mode=runtime_mode,
    )
    return service.reply_transaction_dispute(
        actor=actor,
        dispute_uuid=dispute["dispute_uuid"],
        statement=statement,
        evidence=evidence_items,
        public_key_jwk=_public_jwk(to_key),
        signature=_signature(to_key, payload),
        signature_nonce=nonce,
    )


def _vote_yes_until_passed(service, proposal_uuid, count):
    result = None
    for user_id in range(1, count + 1):
        role = "super_admin" if user_id == 1 else ("manager" if user_id == 2 else "user")
        username = "root" if user_id == 1 else f"user{user_id}"
        result = service.cast_governance_vote(
            actor=_actor(user_id, username, role),
            proposal_uuid=proposal_uuid,
            vote="yes",
        )
    return result


def _assert_address_dispute_payload_is_deidentified(payload):
    forbidden_keys = {
        "user_id",
        "username",
        "email",
        "ip",
        "ip_address",
        "client_ip",
        "reporter_user_id",
        "reporter_username",
    }

    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert str(key).lower() not in forbidden_keys
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)


def _official_treasury_grant_via_governance(service, *, destination_wallet_address, amount, request_uuid):
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    root_wallet = _official_hot_wallet(service, 1)
    manager_wallet = _official_hot_wallet(service, 2)
    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination_wallet_address,
        amount=amount,
        reason="governance official grant",
        reference=request_uuid,
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    passed = service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes")
    assert passed["proposal"]["status"] == "passed"
    service.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    signed = service.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])
    assert signed["multisig"]["ready"] is True
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    return executed["result"]["transfer"]


def _pass_treasury_proposal(service, proposal_uuid):
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    passed = service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes")
    assert passed["proposal"]["status"] == "passed"
    return passed


def _set_governance_timelock_ready(service, proposal_uuid):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
            (proposal_uuid,),
        ).fetchone()
        payload = json.loads(row["payload_json"]) if row and row["payload_json"] else {}
        if isinstance(payload.get("execution_guard"), dict):
            payload["execution_guard"]["timelock_until"] = "2026-01-01T00:00:00Z"
            payload["execution_guard"]["timelock_ends_at"] = "2026-01-01T00:00:00Z"
        execution_hash = service._governance_execution_payload_hash(
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
            SET timelock_until='2026-01-01T00:00:00Z',
                timelock_ends_at='2026-01-01T00:00:00Z',
                payload_json=?,
                execution_payload_hash=?
            WHERE proposal_uuid=?
            """,
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), execution_hash, proposal_uuid),
        )
        conn.commit()
    finally:
        conn.close()


def _pass_public_constitutional_proposal(service, proposal_uuid):
    actors = [
        _actor(1, "root", "super_admin"),
        _actor(2, "user2", "manager"),
        _actor(3, "user3", "user"),
        _actor(4, "user4", "user"),
        _actor(5, "user5", "user"),
    ]
    result = None
    for actor in actors:
        result = service.cast_governance_vote(actor=actor, proposal_uuid=proposal_uuid, vote="yes")
    assert result["proposal"]["status"] == "passed"
    return result


def _force_supply_expansion_eligibility(service):
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        branch = service._canonical_branch_uuid(conn)
        report = economy_layer_report(conn, chain_secret=service.chain_secret, actor={"role": "system", "id": None}, chain_branch=branch)
        releasable_remaining = int(report["supply"]["releasable_remaining"] or 0)
        if releasable_remaining > 0:
            append_economy_event(
                conn,
                chain_secret=service.chain_secret,
                event_type="mint",
                transaction_type="qa_exhaust_releasable_supply",
                source_fund_key="mint",
                destination_fund_key="official_treasury",
                amount=releasable_remaining,
                idempotency_key="qa:supply-expansion:exhaust-releasable",
                metadata={"fixture": True},
                actor={"role": "system", "id": None},
                chain_branch=branch,
            )
        report = economy_layer_report(conn, chain_secret=service.chain_secret, actor={"role": "system", "id": None}, chain_branch=branch)
        for fund_key in ("official_treasury", "promo_fund", "exchange_fund"):
            balance = int(report["funds"][fund_key]["balance"] or 0)
            if balance > 0:
                append_economy_event(
                    conn,
                    chain_secret=service.chain_secret,
                    event_type="burn",
                    transaction_type=f"qa_drain_{fund_key}_for_supply_expansion",
                    source_fund_key=fund_key,
                    destination_fund_key="burn",
                    amount=balance,
                    idempotency_key=f"qa:supply-expansion:drain:{fund_key}",
                    metadata={"fixture": True},
                    actor={"role": "system", "id": None},
                    chain_branch=branch,
                )
        conn.commit()
    finally:
        conn.close()


def _activate_recovery_branch(service, *, incident_tx_hash="incident-hash", excluded_tx_hashes=None, recovery_strategy="treasury_compensation"):
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    created = service.create_emergency_recovery_branch_proposal(
        actor=root,
        incident_tx_hash=incident_tx_hash,
        reason="critical exploit requires recovery branch",
        excluded_tx_hashes=excluded_tx_hashes or [incident_tx_hash],
        recovery_strategy=recovery_strategy,
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes")
    return service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)["result"]


def _activate_tainted_remainder_recovery(service, *, incident_tx_hash, victim_wallet_address, claim_amount, claim_id):
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    created = service.create_emergency_recovery_branch_proposal(
        actor=root,
        incident_tx_hash=incident_tx_hash,
        reason="user-caused compromise; only unused attacker balance should be returned",
        excluded_tx_hashes=[incident_tx_hash],
        recovery_strategy="tainted_remainder_return",
        loss_cause="private_key_leak",
        victim_claims=[
            {
                "claim_id": claim_id,
                "wallet_address": victim_wallet_address,
                "claim_amount_points": claim_amount,
                "review_status": "approved",
                "statement": "approved victim claim for tainted remainder recovery",
            }
        ],
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes", recovery_choice="tainted_remainder_return")
    service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes", recovery_choice="tainted_remainder_return")
    return service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)["result"]


def test_root_scam_address_label_requires_multi_user_vote_before_execute(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    address = "pc1" + ("1" * 48)

    created = service.create_address_risk_proposal(
        actor=root,
        wallet_address=address,
        reason="fraud evidence from explorer tx hash",
        evidence=["tx-hash-1"],
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    assert created["proposal"]["status"] == "voting"
    assert created["proposal"]["quorum_count"] == 5

    with pytest.raises(ValueError, match="execution requires passed"):
        service.execute_governance_proposal(actor=root, proposal_uuid=proposal_uuid)

    one_vote = service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    assert one_vote["proposal"]["status"] == "voting"
    assert one_vote["proposal"]["yes_count"] == 1

    passed = _vote_yes_until_passed(service, proposal_uuid, 5)
    assert passed["proposal"]["status"] == "passed"

    executed = service.execute_governance_proposal(actor=root, proposal_uuid=proposal_uuid)
    assert executed["result"]["action"] == "address_risk_label_applied"
    wallet = service.explorer_wallet(address)["wallet"]
    assert wallet["risk_label"]["risk_level"] == "confirmed_scam"
    assert wallet["risk_label"]["proposal_uuid"] == proposal_uuid
    assert service.verify_governance_audit()["ok"] is True


def test_public_governance_quorum_scales_with_eligible_voter_population(tmp_path):
    service = _service(tmp_path, user_count=100)
    created = service.create_address_risk_proposal(
        actor=_actor(1, "root", "super_admin"),
        wallet_address="pc1" + ("9" * 48),
        reason="fraud evidence from explorer tx hash",
        evidence=["tx-hash-scale-quorum"],
    )

    assert created["proposal"]["eligible_voter_count"] == 100
    assert created["proposal"]["quorum_count"] == 30
    assert created["proposal"][_rate_field("quorum")] == 3000


def test_manager_can_cancel_unexecuted_governance_proposal_with_audit(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    created = service.create_address_risk_proposal(
        actor=root,
        wallet_address="pc1" + ("2" * 48),
        reason="duplicate or stale evidence",
        evidence=["stale-tx-ref"],
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]

    cancelled = service.cancel_governance_proposal(
        actor=manager,
        proposal_uuid=proposal_uuid,
        reason="superseded by better incident report",
    )

    assert cancelled["proposal"]["status"] == "cancelled"
    assert cancelled["proposal"]["lifecycle_status"] == "CANCELLED"
    with pytest.raises(ValueError, match="execution requires passed"):
        service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    assert service.verify_governance_audit()["ok"] is True


def test_public_governance_root_has_no_veto_and_user_proposal_requires_sponsor(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    address = "pc1" + ("2" * 48)

    created = service.create_public_governance_proposal(
        actor=_actor(3, "user3"),
        action_type="MARK_SCAM",
        title="user scam report",
        target_address=address,
        reason="fraud evidence from explorer tx hash",
        evidence=["tx-hash-user-report"],
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    assert created["proposal"]["lifecycle_status"] == "REVIEW"
    assert created["proposal"]["sponsor_required"] is True
    with pytest.raises(ValueError, match="does not accept votes"):
        service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    with pytest.raises(PermissionError, match="not allowed"):
        service.veto_governance_proposal(actor=root, proposal_uuid=proposal_uuid, reason="cannot veto public")

    sponsored = service.sponsor_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    assert sponsored["proposal"]["lifecycle_status"] == "VOTING"
    _vote_yes_until_passed(service, proposal_uuid, 5)
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    assert executed["result"]["action"] == "address_risk_label_applied"


def test_public_governance_proposal_requires_trusted_member_level(tmp_path):
    service = _service(tmp_path, user_count=10)
    address = "pc1" + ("9" * 48)

    with pytest.raises(PermissionError, match="trusted member level"):
        service.create_public_governance_proposal(
            actor=_actor(4, "user4", effective_level="normal"),
            action_type="MARK_SCAM",
            title="normal member scam report",
            target_address=address,
            reason="fraud evidence from explorer tx hash",
            evidence=["tx-hash-normal-report"],
        )


def test_public_governance_spam_duplicate_and_rollback_entry_are_blocked(tmp_path):
    service = _service(tmp_path, user_count=10)
    trusted = _actor(3, "user3", effective_level="trusted")

    first_address = "pc1" + ("a" * 48)
    first = service.create_public_governance_proposal(
        actor=trusted,
        action_type="MARK_SCAM",
        title="trusted scam report",
        target_address=first_address,
        reason="fraud evidence from explorer tx hash",
        evidence=["tx-hash-dup-report"],
    )
    assert first["proposal"]["lifecycle_status"] == "REVIEW"
    with pytest.raises(ValueError, match="similar active proposal"):
        service.create_public_governance_proposal(
            actor=trusted,
            action_type="MARK_SCAM",
            title="duplicate scam report",
            target_address=first_address,
            reason="same active scam report should be rejected",
            evidence=["tx-hash-dup-report-2"],
        )

    for index in range(2):
        service.create_public_governance_proposal(
            actor=trusted,
            action_type="MARK_SCAM",
            title=f"trusted scam report {index}",
            target_address="pc1" + (str(index + 1) * 48),
            reason="fraud evidence from explorer tx hash",
            evidence=[f"tx-hash-rate-{index}"],
        )
    with pytest.raises(ValueError, match="rate limit"):
        service.create_public_governance_proposal(
            actor=trusted,
            action_type="MARK_SCAM",
            title="fourth public proposal",
            target_address="pc1" + ("4" * 48),
            reason="rate limit should reject fourth public proposal",
            evidence=["tx-hash-rate-4"],
        )
    with pytest.raises(PermissionError, match="rollback"):
        service.create_public_governance_proposal(
            actor=trusted,
            action_type="ROLLBACK_BRANCH",
            title="public rollback spam",
            reason="rollback cannot be filed through public entry",
            incident_tx_hash="tx-hash-rollback-spam",
        )


def test_official_treasury_governance_root_can_veto_and_direct_grant_is_blocked(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    _official_hot_wallet(service, 1)
    _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 3)

    with pytest.raises(PermissionError, match="requires executed governance proposal"):
        service.official_wallet_grant(
            actor=root,
            destination_wallet_address=destination["address"],
            amount=5,
            reason="direct grant must fail",
            request_uuid="direct-grant-blocked",
        )

    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination["address"],
        amount=7,
        reason="contest payout",
        action_type="CONTEST_REWARD_PAYOUT",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    assert created["proposal"]["governance_domain"] == "OFFICIAL_TREASURY"
    assert created["proposal"]["root_veto_allowed"] is True
    _pass_treasury_proposal(service, proposal_uuid)
    vetoed = service.veto_governance_proposal(actor=root, proposal_uuid=proposal_uuid, reason="budget freeze")
    assert vetoed["proposal"]["lifecycle_status"] == "VETOED"
    with pytest.raises(ValueError, match="execution requires passed"):
        service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)


def test_governance_list_hides_proposals_actor_cannot_vote_on(tmp_path):
    service = _service(tmp_path, user_count=10)
    manager = _actor(2, "user2", "manager")
    trusted_user = _actor(3, "user3", "user", effective_level="trusted")
    _official_hot_wallet(service, 1)
    _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 4)

    treasury = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination["address"],
        amount=11,
        reason="manager-only voter visibility test",
        action_type="CONTEST_REWARD_PAYOUT",
    )["proposal"]
    public = service.create_public_governance_proposal(
        actor=trusted_user,
        action_type="MARK_SCAM",
        title="public voter visibility test",
        target_address="pc1" + ("8" * 48),
        reason="trusted users can vote on public governance",
        evidence=["visibility-public-evidence"],
    )["proposal"]

    manager_uuids = {
        item["proposal_uuid"]
        for item in service.list_governance_proposals(actor=manager, limit=20)["proposals"]
    }
    user_uuids = {
        item["proposal_uuid"]
        for item in service.list_governance_proposals(actor=trusted_user, limit=20)["proposals"]
    }

    assert treasury["proposal_uuid"] in manager_uuids
    assert public["proposal_uuid"] in manager_uuids
    assert public["proposal_uuid"] in user_uuids
    assert treasury["proposal_uuid"] not in user_uuids


def test_official_treasury_requires_multisig_after_governance_passes(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    root_wallet = _official_hot_wallet(service, 1)
    manager_wallet = _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 3)

    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination["address"],
        amount=9,
        reason="multisig threshold test",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    _pass_treasury_proposal(service, proposal_uuid)
    with pytest.raises(PermissionError, match="multisig threshold not reached"):
        service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)

    one = service.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    assert one["multisig"]["ready"] is False
    assert one["multisig"]["signature_weight"] >= 1
    assert one["multisig"]["threshold_weight"] >= 1
    assert one["proposal"]["payload"]["official_multisig_policy"]["signer_identity_model"] == "manager_identity_distinct_from_treasury_signer_wallet_v1"
    assert one["proposal"]["payload"]["official_multisig_policy"]["wallet_type"] == "official_treasury_multisig"
    center = service.official_treasury_signer_center(actor=manager)
    assert center["official_wallet"]["wallet_type"] == "official_treasury_multisig"
    assert center["policy"]["wallet_scope"] == "official_treasury"
    assert center["fund_addresses"]["official_treasury"].startswith("pc0")
    assert center["fund_addresses"]["exchange_fund"].startswith("pc0")
    assert center["signable"][0]["proposal_uuid"] == proposal_uuid
    two = service.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])
    assert two["multisig"]["ready"] is True
    assert two["multisig"]["signature_weight"] >= two["multisig"]["threshold_weight"]
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    assert executed["result"]["action"] == "official_treasury_transfer_submitted"


def test_official_treasury_signer_center_exposes_offline_payload_verifier(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    root_wallet = _official_hot_wallet(service, 1)
    _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 3)
    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination["address"],
        amount=33,
        reason="signer center offline verifier test",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    _pass_treasury_proposal(service, proposal_uuid)
    center = service.official_treasury_signer_center(actor=root)
    signable = next(item for item in center["signable"] if item["proposal_uuid"] == proposal_uuid)
    assert signable["offline_verifier_model"] == "canonical_json_sha256_v1"
    assert signable["signing_payload"]["proposal_uuid"] == proposal_uuid
    assert signable["signing_payload"]["execution_payload_hash"] == signable["execution_payload_hash"]
    assert signable["signing_payload_hash"] == sha256_text(canonical_json(signable["signing_payload"]))
    assert signable["current_signer_wallet_address"] == root_wallet["address"]


def test_official_treasury_signer_center_reports_service_fee_income(tmp_path):
    service = _service(tmp_path, user_count=5)
    manager = _actor(2, "user2", "manager")
    user = _actor(3, "user3", "user")
    source = _official_hot_wallet(service, 3)
    service.record_transaction(
        user_id=3,
        currency_type="points",
        direction="credit",
        amount=150,
        action_type="user_initial_grant",
        idempotency_key="treasury-analysis-user-funding",
        actor=user,
    )

    service.spend_points(
        user_id=3,
        item_key="post_pin_24h",
        source_wallet_address=source["address"],
        request_uuid="treasury-analysis-service-fee",
        idempotency_key="treasury-analysis-service-fee",
        actor=user,
    )
    service.record_transaction(
        user_id=1,
        currency_type="points",
        direction="credit",
        amount=5,
        action_type="video_tip_platform_fee",
        reference_type="video",
        reference_id="unit-video",
        idempotency_key="treasury-analysis-video-tip-fee",
        public_metadata={"from_user_id": 3, "to_user_id": 4, "fee_points": 5},
        actor=user,
    )

    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        events = conn.execute(
            """
            SELECT transaction_type, destination_fund_key, amount
            FROM points_economy_events
            WHERE transaction_type IN ('service_fee_internal_debit:post_pin_24h', 'video_tip_platform_fee')
            ORDER BY id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    assert [(row["transaction_type"], row["destination_fund_key"], int(row["amount"])) for row in events] == [
        ("service_fee_internal_debit:post_pin_24h", "official_treasury", 100),
        ("video_tip_platform_fee", "official_treasury", 5),
    ]

    center = service.official_treasury_signer_center(actor=manager)
    analysis = center["treasury_analysis"]
    assert analysis["settlement_policy"]["service_fee_ledger_action"] == "service_fee_internal_debit"
    assert analysis["settlement_policy"]["service_fee_destination_fund_key"] == "official_treasury"
    assert analysis["summary"]["service_fee_revenue_points"] == 100
    assert analysis["flow_summary"]["total_inflow_points"] >= 105
    assert any(item["category_key"] == "service_fee_internal_debit:post_pin_24h" for item in analysis["flow_summary"]["categories"])
    assert any(item["category_key"] == "video_tip_platform_fee" for item in analysis["flow_summary"]["categories"])
    assert any(item["category_key"] == "post_pin_24h" and item["inflow_points"] == 100 for item in analysis["service_fee_flow_categories"])
    assert "next_service_fee_settlement_remaining_points" not in analysis["summary"]
    assert "pending_service_fee_points" not in analysis["summary"]
    assert any(item["transaction_type"] == "service_fee_internal_debit:post_pin_24h" for item in analysis["income_categories"])
    assert any(item["item_key"] == "post_pin_24h" for item in analysis["service_fee_items"])
    assert "pricing_fit" not in analysis
    assert analysis["period"]["kind"] == "calendar_month"
    bridge = center["economy_layer"]["legacy_bridge"]
    assert bridge["member_internal_circulating_points"] == 50
    assert bridge["member_internal_available_points"] == 50
    assert bridge["total_internal_circulating_points"] == 50


def test_mint_bootstrap_allocation_is_not_treasury_income(tmp_path):
    service = _service(tmp_path, user_count=5)
    manager = _actor(2, "user2", "manager")

    center = service.official_treasury_signer_center(actor=manager)
    analysis = center["treasury_analysis"]

    assert analysis["summary"]["income_total_points"] == 0
    assert analysis["summary"]["non_operating_mint_allocation_points"] == 10_000_000
    assert not any(item["transaction_type"] == "treasury_allocation" for item in analysis["income_categories"])
    assert any(item["transaction_type"] == "treasury_allocation" for item in analysis["non_operating_mint_categories"])
    assert any(item["transaction_type"] == "treasury_allocation" for item in analysis["recent_non_operating_mint_events"])


def test_mint_request_requires_idempotency_caps_multisig_and_explorer_event(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    root_wallet = _official_hot_wallet(service, 1)
    manager_wallet = _official_hot_wallet(service, 2)

    with pytest.raises(ValueError, match="idempotency"):
        service.create_mint_request_proposal(
            actor=manager,
            destination_fund_key="official_treasury",
            amount=100,
            reason="mint without idempotency key",
            reference="",
        )
    with pytest.raises(ValueError, match="per-proposal cap"):
        service.create_mint_request_proposal(
            actor=manager,
            destination_fund_key="official_treasury",
            amount=1_000_001,
            reason="mint cap should reject oversized proposal",
            reference="mint-cap-test-oversized",
        )

    created = service.create_mint_request_proposal(
        actor=manager,
        destination_fund_key="official_treasury",
        amount=123,
        reason="mint guarded by governance and multisig",
        reference="mint-idem-rc1-001",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    assert created["proposal"]["action_type"] == "MINT_REQUEST"
    assert created["proposal"]["root_veto_allowed"] is True
    assert created["proposal"]["target_wallet_address"].startswith("pc0")
    with pytest.raises(ValueError, match="idempotency"):
        service.create_mint_request_proposal(
            actor=manager,
            destination_fund_key="official_treasury",
            amount=123,
            reason="duplicate mint idempotency key",
            reference="mint-idem-rc1-001",
        )
    _pass_treasury_proposal(service, proposal_uuid)
    with pytest.raises(PermissionError, match="multisig threshold not reached"):
        service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)

    service.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    signed = service.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])
    assert signed["multisig"]["ready"] is True
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    assert executed["result"]["action"] == "mint_executed"
    assert executed["result"]["created"] is True
    event_uuid = executed["result"]["event_uuid"]

    tx = service.explorer_transaction(event_uuid)["transaction"]
    assert tx["wallet_flow"]["source_fund_key"] == "mint"
    assert tx["wallet_flow"]["destination_fund_key"] == "official_treasury"
    assert tx["amount"] == 123
    conn = service.get_db()
    try:
        report = economy_layer_report(conn, chain_secret=service.chain_secret)
    finally:
        conn.close()
    assert report["supply"]["minted_total"] == 20_000_123
    assert report["funds"]["official_treasury"]["balance"] == 10_000_123


def test_mint_reports_supply_exhausted_when_releasable_supply_is_used(tmp_path):
    service = _service(tmp_path, user_count=10)
    manager = _actor(2, "user2", "manager")
    _force_supply_expansion_eligibility(service)

    with pytest.raises(ValueError, match="mint_supply_exhausted"):
        service.create_mint_request_proposal(
            actor=manager,
            destination_fund_key="official_treasury",
            amount=1,
            reason="mint should fail once releasable supply is exhausted",
            reference="mint-after-exhausted",
        )


def test_supply_expansion_request_requires_exhausted_supply_and_critical_funds(tmp_path):
    service = _service(tmp_path, user_count=10)
    manager = _actor(2, "user2", "manager")

    with pytest.raises(ValueError, match="exhausted mint or releasable supply"):
        service.create_supply_expansion_request_proposal(
            actor=manager,
            requested_delta=1_000,
            destination_fund_key="promo_fund",
            reason="should require exhausted supply before constitutional expansion",
            reference="supply-expansion-not-exhausted",
        )

    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        branch = service._canonical_branch_uuid(conn)
        report = economy_layer_report(conn, chain_secret=service.chain_secret, actor={"role": "system", "id": None}, chain_branch=branch)
        append_economy_event(
            conn,
            chain_secret=service.chain_secret,
            event_type="mint",
            transaction_type="qa_exhaust_releasable_without_drain",
            source_fund_key="mint",
            destination_fund_key="official_treasury",
            amount=int(report["supply"]["releasable_remaining"]),
            idempotency_key="qa:supply-expansion:exhaust-without-drain",
            metadata={"fixture": True},
            actor={"role": "system", "id": None},
            chain_branch=branch,
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(ValueError, match="official treasury below minimum"):
        service.create_supply_expansion_request_proposal(
            actor=manager,
            requested_delta=1_000,
            destination_fund_key="promo_fund",
            reason="should require official treasury below minimum reserve",
            reference="supply-expansion-treasury-still-funded",
        )


def test_supply_expansion_is_constitutional_policy_change_not_mint(tmp_path):
    service = _service(tmp_path, user_count=10)
    manager = _actor(2, "user2", "manager")
    _force_supply_expansion_eligibility(service)

    with pytest.raises(ValueError, match="single proposal cap"):
        service.create_supply_expansion_request_proposal(
            actor=manager,
            requested_delta=1_000_001,
            destination_fund_key="promo_fund",
            reason="single expansion cap should block oversized monetary amendment",
            reference="supply-expansion-over-single-cap",
        )

    created = service.create_supply_expansion_request_proposal(
        actor=manager,
        requested_delta=1_000_000,
        destination_fund_key="promo_fund",
        reason="constitutional expansion after exhausted supply and critical official funds",
        reference="supply-expansion-valid-001",
        financial_report="mint exhausted; official, promo, and exchange funds are below critical operating watermarks",
        risk_disclosure="dilutes all holders by one percent of original max supply",
    )
    proposal = created["proposal"]
    proposal_uuid = proposal["proposal_uuid"]
    assert proposal["governance_domain"] == "PROTOCOL_PARAMETER"
    assert proposal["action_type"] == "PARAMETER_CHANGE"
    assert proposal["proposal_severity"] == "CRITICAL"
    assert proposal["root_veto_allowed"] is False
    assert proposal["payload"]["proposal_type"] == "SUPPLY_EXPANSION_REQUEST"
    assert proposal["payload"]["execution_class"] == "MONETARY_POLICY_AMENDMENT"
    assert proposal["payload"]["requested_new_max_supply"] == 101_000_000
    assert proposal["quorum_count"] == 5
    assert proposal[_rate_field("pass_threshold")] == 8000
    assert proposal[_rate_field("vote_differential_required")] == 5000

    _pass_public_constitutional_proposal(service, proposal_uuid)
    with pytest.raises(ValueError, match="timelock active"):
        service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    _set_governance_timelock_ready(service, proposal_uuid)
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    assert executed["result"]["action"] == "max_supply_expanded"
    assert executed["result"]["minted"] is False
    assert executed["result"]["old_max_supply"] == 100_000_000
    assert executed["result"]["new_max_supply"] == 101_000_000

    conn = service.get_db()
    try:
        report = economy_layer_report(conn, chain_secret=service.chain_secret)
    finally:
        conn.close()
    assert report["supply"]["max_supply"] == 101_000_000
    assert report["supply"]["minted_total"] == 60_000_000
    assert report["supply"]["releasable_remaining"] == 1_000_000

    with pytest.raises(ValueError, match="destination violates supply expansion restriction"):
        service.create_mint_request_proposal(
            actor=manager,
            destination_fund_key="official_treasury",
            amount=1,
            reason="expanded supply cannot be minted to the wrong fund",
            reference="mint-expanded-wrong-destination",
        )
    _official_hot_wallet(service, 1)
    _official_hot_wallet(service, 2)
    mint = service.create_mint_request_proposal(
        actor=manager,
        destination_fund_key="promo_fund",
        amount=1,
        reason="expanded supply follows the approved destination fund restriction",
        reference="mint-expanded-promo-destination",
    )
    assert mint["proposal"]["action_type"] == "MINT_REQUEST"
    assert mint["proposal"]["payload"]["destination_fund_key"] == "promo_fund"


def test_generic_parameter_change_cannot_directly_change_max_supply(tmp_path):
    service = _service(tmp_path, user_count=10)
    manager = _actor(2, "user2", "manager")
    created = service.create_policy_governance_proposal(
        actor=manager,
        action_type="PARAMETER_CHANGE",
        title="Attempt direct max supply edit",
        reason="generic parameter changes must not mutate constitutional supply",
        payload={"parameter_key": "max_supply", "parameter_value": "999999999"},
        reference="generic-max-supply-change",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    _pass_public_constitutional_proposal(service, proposal_uuid)
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    assert executed["result"]["action"] == "protocol_policy_approved"

    conn = service.get_db()
    try:
        report = economy_layer_report(conn, chain_secret=service.chain_secret)
    finally:
        conn.close()
    assert report["supply"]["max_supply"] == 100_000_000


def test_revoked_treasury_signer_signature_stops_counting(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    root_wallet = _official_hot_wallet(service, 1)
    manager_wallet = _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 3)

    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination["address"],
        amount=9,
        reason="revoked signer threshold test",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    _pass_treasury_proposal(service, proposal_uuid)
    service.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    ready = service.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])
    assert ready["multisig"]["ready"] is True

    conn = service.get_db()
    try:
        conn.execute("UPDATE points_wallet_identities SET status='revoked' WHERE address=?", (root_wallet["address"],))
        conn.commit()
    finally:
        conn.close()

    status = service.list_governance_proposals(actor=manager)["proposals"][0]["multisig"]
    assert status["ready"] is False
    assert status["signature_count"] == 1
    with pytest.raises(PermissionError, match="disabled or revoked"):
        service.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    with pytest.raises(PermissionError, match="multisig threshold not reached"):
        service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)


def test_multisig_and_voter_snapshot_ignore_new_manager_after_proposal_creation(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    root_wallet = _official_hot_wallet(service, 1)
    manager_wallet = _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 3)

    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination["address"],
        amount=12,
        reason="snapshot should freeze voter and signer set",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    initial_policy = created["proposal"]["multisig"]["policy"]
    assert initial_policy["signer_count"] == 2
    assert created["proposal"]["eligible_voter_count"] == 2

    conn = service.get_db()
    try:
        conn.execute(
            "UPDATE users SET role='manager', member_level='vip', base_level='vip', effective_level='vip' WHERE id=3"
        )
        new_manager_wallet = create_official_hot_wallet(conn, user_id=3, chain_secret=service.chain_secret)
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PermissionError, match="not eligible"):
        service.cast_governance_vote(actor=_actor(3, "user3", "manager"), proposal_uuid=proposal_uuid, vote="yes")
    _pass_treasury_proposal(service, proposal_uuid)
    with pytest.raises(PermissionError, match="not in official multisig policy"):
        service.sign_governance_multisig(
            actor=_actor(3, "user3", "manager"),
            proposal_uuid=proposal_uuid,
            signer_wallet_address=new_manager_wallet["address"],
        )

    service.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    ready = service.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])
    assert ready["multisig"]["ready"] is True
    assert ready["multisig"]["policy"]["signer_count"] == 2


def test_duplicate_sign_and_execute_races_are_idempotent(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    root_wallet = _official_hot_wallet(service, 1)
    manager_wallet = _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 3)

    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination["address"],
        amount=14,
        reason="concurrent execute must be single effect",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    _pass_treasury_proposal(service, proposal_uuid)

    with ThreadPoolExecutor(max_workers=2) as pool:
        sign_results = list(pool.map(
            lambda _idx: service.sign_governance_multisig(
                actor=root,
                proposal_uuid=proposal_uuid,
                signer_wallet_address=root_wallet["address"],
            ),
            range(2),
        ))
    assert sign_results[-1]["multisig"]["signature_count"] == 1
    service.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])

    def execute_once():
        try:
            return ("ok", service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid))
        except Exception as exc:
            return ("err", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _idx: execute_once(), range(2)))
    assert [kind for kind, _payload in results].count("ok") == 1
    assert [kind for kind, _payload in results].count("err") == 1
    conn = service.get_db()
    try:
        rows = conn.execute(
            "SELECT COUNT(*) AS c FROM points_chain_transfer_requests WHERE request_uuid=?",
            (f"governance:{proposal_uuid}:official_transfer",),
        ).fetchone()
        assert int(rows["c"] or 0) == 1
    finally:
        conn.close()


def test_manager_private_wallet_is_not_official_source_for_treasury_execution(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    root_wallet = _official_hot_wallet(service, 1)
    manager_wallet = _official_hot_wallet(service, 2)

    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=manager_wallet["address"],
        amount=11,
        reason="approved manager expense reimbursement",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    _pass_treasury_proposal(service, proposal_uuid)
    service.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    service.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    transfer = executed["result"]["transfer"]["transaction"]
    assert transfer["wallet_flow"]["source_fund_key"] == "official_treasury"
    assert transfer["wallet_flow"]["source_wallet_address"] != manager_wallet["address"]
    assert transfer["wallet_flow"]["destination_wallet_address"] == manager_wallet["address"]


def test_governance_execute_rejects_payload_tamper_and_active_timelock(tmp_path):
    service = _service(tmp_path, user_count=10)
    manager = _actor(2, "user2", "manager")
    _official_hot_wallet(service, 1)
    _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 3)
    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination["address"],
        amount=13,
        reason="tamper test",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    _pass_treasury_proposal(service, proposal_uuid)
    conn = service.get_db()
    try:
        conn.execute("UPDATE points_chain_governance_proposals SET payload_json='{\"memo\":\"tampered\"}' WHERE proposal_uuid=?", (proposal_uuid,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError, match="payload hash mismatch"):
        service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)

    prod_service = _service(tmp_path / "prod", user_count=10, mode="production")
    _official_hot_wallet(prod_service, 1)
    _official_hot_wallet(prod_service, 2)
    prod_destination = _official_hot_wallet(prod_service, 3)
    prod_created = prod_service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=prod_destination["address"],
        amount=17,
        reason="production timelock test",
    )
    prod_uuid = prod_created["proposal"]["proposal_uuid"]
    _pass_treasury_proposal(prod_service, prod_uuid)
    with pytest.raises(ValueError, match="timelock active"):
        prod_service.execute_governance_proposal(actor=manager, proposal_uuid=prod_uuid)


def test_governance_deadline_guard_rejects_timelock_row_tamper(tmp_path):
    service = _service(tmp_path, user_count=10, mode="production")
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    root_wallet = _official_hot_wallet(service, 1)
    manager_wallet = _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 3)
    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination["address"],
        amount=25,
        reason="deadline guard tamper test",
        reference="deadline-guard",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    _pass_treasury_proposal(service, proposal_uuid)
    service.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    service.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_governance_proposals SET timelock_until='2026-01-01T00:00:00Z', timelock_ends_at='2026-01-01T00:00:00Z' WHERE proposal_uuid=?",
            (proposal_uuid,),
        )
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(ValueError, match="execution guard mismatch: timelock_until"):
        service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)


def test_governance_clock_fast_forward_enters_safe_mode(tmp_path, monkeypatch):
    service = _service(tmp_path, user_count=10, mode="production")
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    root_wallet = _official_hot_wallet(service, 1)
    manager_wallet = _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 3)
    created = service.create_treasury_transfer_proposal(
        actor=manager,
        destination_wallet_address=destination["address"],
        amount=25,
        reason="clock attack timelock probe",
        reference="clock-attack",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    _pass_treasury_proposal(service, proposal_uuid)
    service.sign_governance_multisig(actor=root, proposal_uuid=proposal_uuid, signer_wallet_address=root_wallet["address"])
    signed = service.sign_governance_multisig(actor=manager, proposal_uuid=proposal_uuid, signer_wallet_address=manager_wallet["address"])
    assert signed["multisig"]["ready"] is True

    baseline_wall = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    service._governance_clock_wall = baseline_wall
    service._governance_clock_monotonic = 1000.0

    class FastForwardDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = baseline_wall + timedelta(hours=13)
            if tz is None:
                return value.replace(tzinfo=None)
            return value.astimezone(tz)

    class SlowMonotonic:
        @staticmethod
        def monotonic():
            return 1005.0

    monkeypatch.setattr(points_service_module, "datetime", FastForwardDateTime)
    monkeypatch.setattr(points_service_module, "_monotonic_time", SlowMonotonic)

    with pytest.raises(ValueError, match="governance clock guard active"):
        service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)

    safe_mode = service.safe_mode_status()
    assert safe_mode["safe_mode"] is True
    assert safe_mode["reason"] == "governance_clock_jump_detected"
    assert safe_mode["verification"]["violation"] == "wall_clock_fast_forward"


def test_governance_clock_uses_suspend_aware_boottime_when_available(tmp_path, monkeypatch):
    service = _service(tmp_path, user_count=3, mode="production")
    baseline_wall = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    service._governance_clock_wall = baseline_wall
    service._governance_clock_monotonic = 1000.0

    class SuspendedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = baseline_wall + timedelta(hours=13)
            if tz is None:
                return value.replace(tzinfo=None)
            return value.astimezone(tz)

    class BootTimeClock:
        CLOCK_BOOTTIME = 7

        @staticmethod
        def clock_gettime(clock_id):
            assert clock_id == BootTimeClock.CLOCK_BOOTTIME
            return 1000.0 + (13 * 60 * 60)

        @staticmethod
        def monotonic():
            return 1005.0

    monkeypatch.setattr(points_service_module, "datetime", SuspendedDateTime)
    monkeypatch.setattr(points_service_module, "_monotonic_time", BootTimeClock)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        service._governance_assert_clock_safe_locked(conn, "qa_host_suspend")
    finally:
        conn.close()

    assert service.safe_mode_status()["safe_mode"] is False
    assert service._governance_clock_monotonic == 1000.0 + (13 * 60 * 60)


def test_governance_clock_tolerates_small_long_running_host_drift(tmp_path, monkeypatch):
    service = _service(tmp_path, user_count=3, mode="production")
    baseline_wall = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    service._governance_clock_wall = baseline_wall
    service._governance_clock_monotonic = 1000.0

    class DriftedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = baseline_wall + timedelta(seconds=858)
            if tz is None:
                return value.replace(tzinfo=None)
            return value.astimezone(tz)

    class DriftedMonotonic:
        @staticmethod
        def monotonic():
            return 1815.0

    monkeypatch.setattr(points_service_module, "datetime", DriftedDateTime)
    monkeypatch.setattr(points_service_module, "_monotonic_time", DriftedMonotonic)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        service._governance_assert_clock_safe_locked(conn, "qa_host_drift_under_load")
    finally:
        conn.close()

    assert service.safe_mode_status()["safe_mode"] is False
    assert service._governance_clock_monotonic == 1815.0


def test_emergency_recovery_branch_is_governance_branch_not_ledger_rollback(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")

    created = service.create_emergency_recovery_branch_proposal(
        actor=root,
        incident_tx_hash="incident-ledger-hash",
        reason="critical exploit requires governance recovery branch",
        base_block_number=12,
        excluded_tx_hashes=["incident-ledger-hash"],
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    assert created["proposal"]["governance_domain"] == "EMERGENCY_SECURITY"
    assert created["proposal"]["proposal_severity"] == "CRITICAL"
    assert created["proposal"]["root_veto_allowed"] is False
    assert created["proposal"]["quorum_count"] == 2

    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    passed = service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes")
    assert passed["proposal"]["status"] == "passed"

    executed = service.execute_governance_proposal(actor=root, proposal_uuid=proposal_uuid)
    assert executed["result"]["action"] == "canonical_recovery_branch_activated"
    assert executed["result"]["ledger_mutation"] == "forbidden"
    report = service.root_report()
    branch = report["governance"]["branches"][0]
    assert branch["is_canonical"] is True
    assert branch["proposal_uuid"] == proposal_uuid
    assert branch["replay_plan"]["ledger_mutation"] == "forbidden"


def test_recovery_branch_replays_parent_without_stolen_tx_and_isolates_old_assets(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4")
    victim_wallet = _official_hot_wallet(service, 3)
    attacker_wallet, _attacker_key = _self_custody_wallet(service, 4)
    attacker_sink_wallet, _sink_key = _self_custody_wallet(service, 5)

    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="branch-recovery-victim-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    assert service.explorer_wallet(victim_wallet["address"])["wallet"]["points_balance"] == 100

    stolen = service.submit_wallet_transaction(
        actor=victim,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=40,
        fee_points=1,
        request_uuid="stolen-transfer-main-branch",
        memo="compromised account transfer",
    )
    assert stolen["created"] is True
    duplicate = service.submit_wallet_transaction(
        actor=victim,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=40,
        fee_points=1,
        request_uuid="stolen-transfer-main-branch",
        memo="compromised account transfer",
    )
    assert duplicate["created"] is False
    with pytest.raises(ValueError, match="insufficient balance"):
        service.submit_wallet_transaction(
            actor=victim,
            source_wallet_address=victim_wallet["address"],
            destination_wallet_address=attacker_wallet["address"],
            amount_points=61,
            fee_points=1,
            request_uuid="double-spend-different-request-main-branch",
            memo="second settled transaction must respect available balance",
        )
    _force_request_proved(service, stolen["transaction_hash"])
    confirmed_stolen = service.explorer_transaction(stolen["transaction_hash"])["transaction"]
    assert confirmed_stolen["status"] == "confirmed"
    assert confirmed_stolen["chain_branch"] == "main"
    assert service.explorer_wallet(victim_wallet["address"])["wallet"]["points_balance"] == 59
    assert service.explorer_wallet(attacker_wallet["address"])["wallet"]["points_balance"] == 40

    created = service.create_emergency_recovery_branch_proposal(
        actor=root,
        incident_tx_hash=stolen["transaction_hash"],
        reason="critical exploit requires excluding stolen transfer from canonical branch",
        excluded_tx_hashes=[stolen["transaction_hash"]],
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes")
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    seed = executed["result"]["recovery_seed"]
    assert executed["result"]["asset_universe"] == executed["result"]["branch_uuid"]
    assert seed["created_count"] >= 1
    assert seed["excluded_ledger_rows"] >= 3
    assert stolen["transaction_hash"] in seed["excluded_refs"]
    assert any(item["wallet_address"] == victim_wallet["address"] and item["amount_points"] == 100 for item in seed["created"])

    victim_after = service.explorer_wallet(victim_wallet["address"])["wallet"]
    attacker_after = service.explorer_wallet(attacker_wallet["address"])["wallet"]
    assert victim_after["chain_branch"] == executed["result"]["branch_uuid"]
    assert victim_after["points_balance"] == 100
    assert attacker_after["points_balance"] == 0

    with pytest.raises(ValueError, match="non-canonical branch"):
        service.submit_wallet_transaction(
            actor=victim,
            source_wallet_address=victim_wallet["address"],
            destination_wallet_address=attacker_wallet["address"],
            amount_points=40,
            fee_points=1,
            request_uuid="stolen-transfer-main-branch",
            memo="replay old stolen transfer",
        )
    with pytest.raises(ValueError, match="insufficient balance"):
        _signed_wallet_transfer(
            service,
            actor=attacker,
            source_wallet=attacker_wallet,
            source_key=_attacker_key,
            destination_wallet_address=attacker_sink_wallet["address"],
            amount=1,
            fee=0,
            request_uuid="attacker-spends-old-branch-assets",
        )

    post_fork = service.submit_wallet_transaction(
        actor=victim,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=10,
        fee_points=1,
        request_uuid="post-fork-valid-transfer",
    )
    _force_request_proved(service, post_fork["transaction_hash"])
    post_fork_tx = service.explorer_transaction(post_fork["transaction_hash"])["transaction"]
    assert post_fork_tx["status"] == "confirmed"
    assert post_fork_tx["chain_branch"] == executed["result"]["branch_uuid"]
    old_tx = service.explorer_transaction(stolen["transaction_hash"])["transaction"]
    assert old_tx["chain_branch"] == "main"
    assert old_tx["branch"]["is_canonical"] is False
    assert old_tx["branch"]["write_enabled"] is False
    assert service.explorer_wallet(victim_wallet["address"])["wallet"]["points_balance"] == 89
    assert service.explorer_wallet(attacker_wallet["address"])["wallet"]["points_balance"] == 10


def test_recovery_branch_compensation_preserves_later_normal_transactions_and_treasury_absorbs_shortfall(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4")
    normal_sender = _actor(5, "user5")
    normal_receiver = _actor(6, "user6")
    victim_wallet = _official_hot_wallet(service, 3)
    attacker_wallet, _attacker_key = _self_custody_wallet(service, 4)
    innocent_wallet, _innocent_key = _self_custody_wallet(service, 7)
    normal_sender_wallet = _official_hot_wallet(service, 5)
    normal_receiver_wallet = _official_hot_wallet(service, 6)

    grants = [
        _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=victim_wallet["address"],
            amount=100,
            request_uuid="delayed-branch-victim-grant",
        ),
        _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=attacker_wallet["address"],
            amount=5,
            request_uuid="delayed-branch-attacker-legit-grant",
        ),
        _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=normal_sender_wallet["address"],
            amount=30,
            request_uuid="delayed-branch-normal-sender-grant",
        ),
    ]
    for grant in grants:
        _force_request_proved(service, grant["transaction_hash"])
        service.explorer_transaction(grant["transaction_hash"])

    stolen = service.submit_wallet_transaction(
        actor=victim,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=40,
        fee_points=1,
        request_uuid="delayed-branch-stolen-transfer",
        memo="compromised transfer several blocks before discovery",
    )
    _force_request_proved(service, stolen["transaction_hash"])
    service.explorer_transaction(stolen["transaction_hash"])

    normal_after_theft = service.submit_wallet_transaction(
        actor=normal_sender,
        source_wallet_address=normal_sender_wallet["address"],
        destination_wallet_address=normal_receiver_wallet["address"],
        amount_points=7,
        fee_points=1,
        request_uuid="delayed-branch-normal-after-theft",
        memo="legitimate transaction after theft but before recovery",
    )
    _force_request_proved(service, normal_after_theft["transaction_hash"])
    service.explorer_transaction(normal_after_theft["transaction_hash"])

    tainted_spend = _signed_wallet_transfer(
        service,
        actor=attacker,
        source_wallet=attacker_wallet,
        source_key=_attacker_key,
        destination_wallet_address=innocent_wallet["address"],
        amount=30,
        fee=1,
        request_uuid="delayed-branch-tainted-spend-after-theft",
        memo="attacker tries to spend funds that only exist because of the theft",
    )
    _force_request_proved(service, tainted_spend["transaction_hash"])
    service.explorer_transaction(tainted_spend["transaction_hash"])

    created = service.create_emergency_recovery_branch_proposal(
        actor=root,
        incident_tx_hash=stolen["transaction_hash"],
        reason="critical theft found after later normal transactions; preserve unrelated activity and compensate shortfall",
        excluded_tx_hashes=[stolen["transaction_hash"]],
        recovery_strategy="treasury_compensation",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes")
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    seed = executed["result"]["recovery_seed"]

    assert stolen["transaction_hash"] in seed["excluded_refs"]
    assert seed["auto_excluded_refs"] == []
    assert seed["compensation"]["required_total"] == 26
    assert seed["compensation"]["items"][0]["wallet_address"] == attacker_wallet["address"]
    assert seed["compensation"]["created"][0]["fund_key"] == "official_treasury"

    victim_after = service.explorer_wallet(victim_wallet["address"])["wallet"]
    attacker_after = service.explorer_wallet(attacker_wallet["address"])["wallet"]
    innocent_after = service.explorer_wallet(innocent_wallet["address"])["wallet"]
    normal_sender_after = service.explorer_wallet(normal_sender_wallet["address"])["wallet"]
    normal_receiver_after = service.explorer_wallet(normal_receiver_wallet["address"])["wallet"]

    assert victim_after["points_balance"] == 100
    assert attacker_after["points_balance"] == 0
    assert innocent_after["points_balance"] == 30
    assert normal_sender_after["points_balance"] == 23
    assert normal_receiver_after["points_balance"] == 7


def test_recovery_branch_tainted_remainder_returns_only_unused_attacker_balance_to_approved_claims(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    victim_a = _actor(3, "user3", "user", effective_level="trusted")
    victim_b = _actor(4, "user4", "user", effective_level="trusted")
    attacker = _actor(5, "user5")
    victim_a_wallet = _official_hot_wallet(service, 3)
    victim_b_wallet = _official_hot_wallet(service, 4)
    attacker_wallet = _official_hot_wallet(service, 5)
    innocent_wallet = _official_hot_wallet(service, 6)

    grants = [
        _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=victim_a_wallet["address"],
            amount=100,
            request_uuid="tainted-return-victim-a-grant",
        ),
        _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=victim_b_wallet["address"],
            amount=100,
            request_uuid="tainted-return-victim-b-grant",
        ),
        _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=attacker_wallet["address"],
            amount=5,
            request_uuid="tainted-return-attacker-legit-grant",
        ),
    ]
    for grant in grants:
        _force_request_proved(service, grant["transaction_hash"])
        service.explorer_transaction(grant["transaction_hash"])

    theft_a = service.submit_wallet_transaction(
        actor=victim_a,
        source_wallet_address=victim_a_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=40,
        fee_points=1,
        request_uuid="tainted-return-theft-a",
    )
    _force_request_proved(service, theft_a["transaction_hash"])
    service.explorer_transaction(theft_a["transaction_hash"])

    theft_b = service.submit_wallet_transaction(
        actor=victim_b,
        source_wallet_address=victim_b_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=20,
        fee_points=1,
        request_uuid="tainted-return-theft-b",
    )
    _force_request_proved(service, theft_b["transaction_hash"])
    service.explorer_transaction(theft_b["transaction_hash"])

    spend = service.submit_wallet_transaction(
        actor=attacker,
        source_wallet_address=attacker_wallet["address"],
        destination_wallet_address=innocent_wallet["address"],
        amount_points=30,
        fee_points=1,
        request_uuid="tainted-return-attacker-spend",
    )
    _force_request_proved(service, spend["transaction_hash"])
    service.explorer_transaction(spend["transaction_hash"])

    created = service.create_emergency_recovery_branch_proposal(
        actor=root,
        incident_tx_hash=theft_a["transaction_hash"],
        incident_tx_hashes=[theft_b["transaction_hash"]],
        reason="multiple victims report private-key compromise; only unused attacker balance should be returned",
        excluded_tx_hashes=[theft_a["transaction_hash"], theft_b["transaction_hash"]],
        recovery_strategy="tainted_remainder_return",
        loss_cause="private_key_leak",
        victim_claims=[
            {
                "claim_id": "claim-a",
                "wallet_address": victim_a_wallet["address"],
                "claim_amount_points": 40,
                "review_status": "approved",
                "statement": "victim A evidence reviewed",
            },
            {
                "claim_id": "claim-b",
                "wallet_address": victim_b_wallet["address"],
                "claim_amount_points": 20,
                "review_status": "approved",
                "statement": "victim B evidence reviewed",
            },
        ],
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes", recovery_choice="tainted_remainder_return")
    service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes", recovery_choice="tainted_remainder_return")
    executed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)
    seed = executed["result"]["recovery_seed"]

    assert theft_a["transaction_hash"] not in seed["excluded_refs"]
    assert theft_b["transaction_hash"] not in seed["excluded_refs"]
    tainted = seed["tainted_remainder_return"]
    assert tainted["return_amount"] == 30
    assert tainted["distribution"] == [
        {
            "claim_id": "claim-a",
            "wallet_address": victim_a_wallet["address"],
            "claim_amount_points": 40,
            "allocated_points": 20,
        },
        {
            "claim_id": "claim-b",
            "wallet_address": victim_b_wallet["address"],
            "claim_amount_points": 20,
            "allocated_points": 10,
        },
    ]
    assert seed["compensation"]["required_total"] == 0
    assert service.explorer_wallet(victim_a_wallet["address"])["wallet"]["points_balance"] == 80
    assert service.explorer_wallet(victim_b_wallet["address"])["wallet"]["points_balance"] == 90
    assert service.explorer_wallet(attacker_wallet["address"])["wallet"]["points_balance"] == 5
    assert service.explorer_wallet(innocent_wallet["address"])["wallet"]["points_balance"] == 30


def test_transaction_dispute_review_can_create_recovery_governance_proposal(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    manager = _actor(2, "user2", "manager")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    attacker_wallet, _attacker_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="dispute-victim-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=attacker_wallet["address"],
        amount=25,
        fee=1,
        request_uuid="dispute-theft-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    service.explorer_transaction(transfer["transaction_hash"])

    dispute = _address_signed_dispute(
        service,
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        from_wallet=victim_wallet,
        from_key=victim_key,
        to_wallet_address=attacker_wallet["address"],
        amount=25,
        statement="private key phishing claim with evidence",
        evidence=["evidence-ref-1"],
    )["dispute"]
    reviewed = service.review_transaction_dispute(
        actor=manager,
        dispute_uuid=dispute["dispute_uuid"],
        status="approved",
        review_note="manager reviewed claim evidence",
        recommended_strategy="tainted_remainder_return",
        create_proposal=True,
    )

    assert reviewed["dispute"]["status"] == "proposal_created"
    assert reviewed["proposal"]["action_type"] == "ROLLBACK_BRANCH"
    assert reviewed["proposal"]["payload"]["victim_claims"][0]["review_status"] == "approved"
    assert reviewed["proposal"]["payload"]["compensation_policy"]["compensation_rate_per_10000"] == 0
    assert reviewed["dispute"]["suspect_wallet_address"] == attacker_wallet["address"]
    assert reviewed["address_risk_proposal"]["action_type"] == "MARK_SCAM"
    assert reviewed["address_risk_proposal"]["target_wallet_address"] == attacker_wallet["address"]
    assert reviewed["address_freeze_proposal"]["action_type"] == "FREEZE_ADDRESS"
    assert reviewed["address_freeze_proposal"]["target_wallet_address"] == attacker_wallet["address"]
    assert reviewed["dispute"]["address_risk_proposal_uuid"] == reviewed["address_risk_proposal"]["proposal_uuid"]
    assert reviewed["dispute"]["address_freeze_proposal_uuid"] == reviewed["address_freeze_proposal"]["proposal_uuid"]
    assert reviewed["provisional_freeze"]["status"] == "active"
    assert reviewed["provisional_freeze"]["wallet_address"] == attacker_wallet["address"]
    assert reviewed["provisional_freeze"]["linked_proposal_uuid"] == reviewed["address_freeze_proposal"]["proposal_uuid"]

    with pytest.raises(PermissionError, match="temporarily frozen pending governance review"):
        service.submit_wallet_transaction(
            actor=_actor(4, "user4"),
            source_wallet_address=attacker_wallet["address"],
            destination_wallet_address=victim_wallet["address"],
            amount_points=1,
            fee_points=0,
            request_uuid="dispute-temp-freeze-blocked",
        )

    freeze_uuid = reviewed["address_freeze_proposal"]["proposal_uuid"]
    _vote_yes_until_passed(service, freeze_uuid, 5)
    executed_freeze = service.execute_governance_proposal(actor=manager, proposal_uuid=freeze_uuid)
    assert executed_freeze["result"]["action"] == "wallet_address_frozen"
    assert service.explorer_wallet(attacker_wallet["address"])["wallet"]["governance_freeze"]["freeze_type"] == "governance"


def test_transaction_dispute_cancel_releases_provisional_freeze(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    root = _actor(1, "root", "super_admin")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    attacker_wallet, _attacker_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="dispute-cancel-victim-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=attacker_wallet["address"],
        amount=25,
        fee=1,
        request_uuid="dispute-cancel-theft-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    service.explorer_transaction(transfer["transaction_hash"])

    dispute = _address_signed_dispute(
        service,
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        from_wallet=victim_wallet,
        from_key=victim_key,
        to_wallet_address=attacker_wallet["address"],
        amount=25,
        statement="private key phishing claim with evidence",
        evidence=["evidence-ref-1"],
    )["dispute"]
    reviewed = service.review_transaction_dispute(
        actor=root,
        dispute_uuid=dispute["dispute_uuid"],
        status="approved",
        review_note="temporary review approval",
        recommended_strategy="tainted_remainder_return",
        create_proposal=True,
    )
    assert reviewed["provisional_freeze"]["status"] == "active"
    assert service.explorer_wallet(attacker_wallet["address"])["wallet"]["governance_freeze"]["freeze_type"] == "provisional"

    cancelled = service.review_transaction_dispute(
        actor=root,
        dispute_uuid=dispute["dispute_uuid"],
        status="cancelled",
        review_note="false positive case",
    )
    assert cancelled["provisional_release"]["released_count"] == 1
    assert service.explorer_wallet(attacker_wallet["address"])["wallet"]["governance_freeze"] is None


def test_address_signed_dispute_hides_reporter_identity_and_freezes_to_for_one_hour(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4", "user", effective_level="trusted")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    attacker_wallet, _attacker_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="address-dispute-victim-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=attacker_wallet["address"],
        amount=25,
        fee=1,
        request_uuid="address-dispute-theft-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    service.explorer_transaction(transfer["transaction_hash"])

    dispute_result = _address_signed_dispute(
        service,
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        from_wallet=victim_wallet,
        from_key=victim_key,
        to_wallet_address=attacker_wallet["address"],
        amount=25,
        statement="Unauthorized transfer opened with anonymous address proof.",
        evidence=["evidence-address-signed"],
    )
    dispute = dispute_result["dispute"]

    assert dispute["from_signature_verified"] is True
    assert dispute["identity_redaction_model"] == "address_proven_anonymous_v1"
    assert "reporter_user_id" not in dispute
    assert "reporter_username" not in dispute
    _assert_address_dispute_payload_is_deidentified(dispute)
    freeze = dispute_result["initial_provisional_freeze"]
    assert freeze["status"] == "active"
    assert freeze["wallet_address"] == attacker_wallet["address"]
    assert freeze["source_dispute_uuid"] == dispute["dispute_uuid"]
    attacker_view = service.explorer_wallet(attacker_wallet["address"])["wallet"]
    assert attacker_view["points_frozen"] == 0
    assert attacker_view["governance_freeze"]["freeze_type"] == "provisional"

    with pytest.raises(PermissionError, match="temporarily frozen pending governance review"):
        service.submit_wallet_transaction(
            actor=attacker,
            source_wallet_address=attacker_wallet["address"],
            destination_wallet_address=victim_wallet["address"],
            amount_points=1,
            fee_points=0,
            request_uuid="address-dispute-outgoing-blocked",
        )

    manager_list = service.list_transaction_disputes(actor=_actor(2, "user2", "manager"))
    assert manager_list["disputes"]
    assert "reporter_user_id" not in manager_list["disputes"][0]
    assert "reporter_username" not in manager_list["disputes"][0]
    _assert_address_dispute_payload_is_deidentified(manager_list)


def test_account_bound_official_hot_wallet_can_open_and_reply_to_dispute_without_private_key(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4", "user", effective_level="trusted")
    unrelated = _actor(5, "user5", "user", effective_level="trusted")
    victim_wallet = _official_hot_wallet(service, 3)
    attacker_wallet = _official_hot_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="official-hot-dispute-victim-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = service.submit_wallet_transaction(
        actor=victim,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=25,
        fee_points=1,
        request_uuid="official-hot-dispute-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    tx = service.explorer_transaction(transfer["transaction_hash"])["transaction"]

    with pytest.raises(ValueError, match="account-bound official hot wallet"):
        service.create_transaction_dispute(
            actor=unrelated,
            tx_hash=transfer["transaction_hash"],
            statement="Unrelated account cannot claim this source address by account binding.",
            victim_wallet_address=victim_wallet["address"],
            claimed_amount_points=25,
            loss_cause="unknown",
            evidence=["official-hot-unrelated"],
            signature_nonce="official-hot-unrelated-open",
            from_wallet_address=victim_wallet["address"],
            to_wallet_address=attacker_wallet["address"],
            chain_branch=tx["chain_branch"],
            account_bound_proof=True,
        )

    result = service.create_transaction_dispute(
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        statement="Official hot source uses account bound address proof without private key.",
        victim_wallet_address=victim_wallet["address"],
        claimed_amount_points=25,
        loss_cause="unknown",
        evidence=["official-hot-account-bound"],
        signature_nonce="official-hot-account-bound-open",
        from_wallet_address=victim_wallet["address"],
        to_wallet_address=attacker_wallet["address"],
        chain_branch=tx["chain_branch"],
        account_bound_proof=True,
    )
    dispute = result["dispute"]
    assert dispute["from_signature_verified"] is True
    assert dispute["signature_purpose"] == "account_bound_dispute_open"
    assert dispute["identity_redaction_model"] == "account_bound_official_hot_v1"
    assert result["initial_provisional_freeze"]["wallet_address"] == attacker_wallet["address"]
    assert service.explorer_wallet(attacker_wallet["address"])["wallet"]["points_frozen"] == 0
    _assert_address_dispute_payload_is_deidentified(dispute)
    assert result["notifications"]["direct_official_hot_owner"] is True
    assert result["notifications"]["created_count"] == 1
    conn = service.get_db()
    try:
        note = conn.execute(
            "SELECT * FROM notifications WHERE type='points_chain_address_dispute_reply_required' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert note is not None
        assert int(note["user_id"]) == attacker["id"]
        assert "被懷疑有詐騙行為" in note["body"]
        assert "請 To 地址擁有者進行回覆" in note["body"]
        assert "若無則逕付治理投票" in note["body"]
        metadata = json.loads(note["metadata_json"])
        assert metadata["action"] == "points_chain_dispute_reply"
        assert metadata["dispute_uuid"] == dispute["dispute_uuid"]
        assert metadata["tx_hash"] == transfer["transaction_hash"]
        assert metadata["to_wallet_address"] == attacker_wallet["address"]
        assert metadata["direct_official_hot_owner"] is True
    finally:
        conn.close()
    manager_disputes = service.list_transaction_disputes(actor=_actor(2, "user2", "manager"))
    assert manager_disputes["official_hot_wallet_labels"][attacker_wallet["address"]] == "user4"
    user_disputes = service.list_transaction_disputes(actor=victim)
    assert "official_hot_wallet_labels" not in user_disputes

    with pytest.raises(ValueError, match="account-bound official hot wallet"):
        service.reply_transaction_dispute(
            actor=unrelated,
            dispute_uuid=dispute["dispute_uuid"],
            statement="Unrelated account cannot reply for this destination address.",
            evidence=["official-hot-unrelated-reply"],
            signature_nonce="official-hot-unrelated-reply",
            account_bound_proof=True,
        )

    replied = service.reply_transaction_dispute(
        actor=attacker,
        dispute_uuid=dispute["dispute_uuid"],
        statement="Official hot destination uses account bound reply proof without private key.",
        evidence=["official-hot-account-bound-reply"],
        signature_nonce="official-hot-account-bound-reply",
        account_bound_proof=True,
    )["dispute"]
    assert replied["reply_signature_verified"] is True
    assert replied["reply_statement_hash"]
    _assert_address_dispute_payload_is_deidentified(replied)
    reviewed = service.review_transaction_dispute(
        actor=_actor(2, "user2", "manager"),
        dispute_uuid=dispute["dispute_uuid"],
        status="approved",
        review_note="Escalate with both sides attached to governance materials.",
        recommended_strategy="tainted_remainder_return",
        create_proposal=True,
    )
    payload = reviewed["proposal"]["payload"]
    assert payload["victim_statement"] == dispute["statement"]
    assert payload["victim_evidence_refs"] == ["official-hot-account-bound"]
    assert payload["counterparty_reply"]["statement"] == replied["reply_statement"]
    assert payload["counterparty_reply"]["evidence_refs"] == ["official-hot-account-bound-reply"]
    assert payload["counterparty_reply"]["address_signature_verified"] is True


def test_dispute_to_non_official_hot_address_sends_sitewide_reply_notice(tmp_path):
    service = _service(tmp_path, user_count=5)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    victim_wallet = _official_hot_wallet(service, 3)
    attacker_wallet, _attacker_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="sitewide-dispute-notice-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = service.submit_wallet_transaction(
        actor=victim,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=25,
        fee_points=1,
        request_uuid="sitewide-dispute-notice-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    tx = service.explorer_transaction(transfer["transaction_hash"])["transaction"]

    result = service.create_transaction_dispute(
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        statement="Official hot source asks sitewide notice for unknown To holder.",
        victim_wallet_address=victim_wallet["address"],
        claimed_amount_points=25,
        loss_cause="unknown",
        evidence=["sitewide-evidence-ref"],
        signature_nonce="sitewide-dispute-notice-open",
        from_wallet_address=victim_wallet["address"],
        to_wallet_address=attacker_wallet["address"],
        chain_branch=tx["chain_branch"],
        account_bound_proof=True,
    )

    assert result["notifications"]["direct_official_hot_owner"] is False
    assert result["notifications"]["created_count"] == 5
    conn = service.get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM notifications WHERE source_ref=? ORDER BY user_id ASC",
            (result["dispute"]["dispute_uuid"],),
        ).fetchall()
        assert [int(row["user_id"]) for row in rows] == [1, 2, 3, 4, 5]
        assert all("sitewide-evidence-ref" in row["body"] for row in rows)
        assert all("若無則逕付治理投票" in row["body"] for row in rows)
        metadata = json.loads(rows[0]["metadata_json"])
        assert metadata["action"] == "points_chain_dispute_reply"
        assert metadata["direct_official_hot_owner"] is False
    finally:
        conn.close()


def test_to_can_reply_after_dispute_has_entered_governance_voting(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4", "user", effective_level="trusted")
    manager = _actor(2, "user2", "manager")
    victim_wallet = _official_hot_wallet(service, 3)
    attacker_wallet = _official_hot_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="late-reply-dispute-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = service.submit_wallet_transaction(
        actor=victim,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=25,
        fee_points=1,
        request_uuid="late-reply-dispute-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    tx = service.explorer_transaction(transfer["transaction_hash"])["transaction"]
    dispute = service.create_transaction_dispute(
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        statement="Victim opens dispute before To has time to reply.",
        victim_wallet_address=victim_wallet["address"],
        claimed_amount_points=25,
        loss_cause="unknown",
        evidence=["late-reply-open-evidence"],
        signature_nonce="late-reply-open",
        from_wallet_address=victim_wallet["address"],
        to_wallet_address=attacker_wallet["address"],
        chain_branch=tx["chain_branch"],
        account_bound_proof=True,
    )["dispute"]
    reviewed = service.review_transaction_dispute(
        actor=manager,
        dispute_uuid=dispute["dispute_uuid"],
        status="approved",
        review_note="Move to voting before To reply arrives.",
        recommended_strategy="tainted_remainder_return",
        create_proposal=True,
    )
    proposal_uuid = reviewed["proposal"]["proposal_uuid"]
    assert reviewed["dispute"]["status"] == "proposal_created"
    assert not reviewed["proposal"]["payload"].get("counterparty_reply")

    replied = service.reply_transaction_dispute(
        actor=attacker,
        dispute_uuid=dispute["dispute_uuid"],
        statement="Late To reply after governance voting started.",
        evidence=["late-reply-counter-evidence"],
        signature_nonce="late-reply-account-bound",
        account_bound_proof=True,
    )["dispute"]
    assert replied["reply_signature_verified"] is True

    proposals = service.list_governance_proposals(actor=manager, limit=20)["proposals"]
    proposal = next(item for item in proposals if item["proposal_uuid"] == proposal_uuid)
    assert proposal["execution_readiness"]["payload_verified"] is True
    assert proposal["payload"]["counterparty_reply_source"] == "latest_dispute_reply_overlay_v1"
    assert proposal["payload"]["counterparty_reply"]["statement"] == replied["reply_statement"]
    assert proposal["payload"]["counterparty_reply"]["evidence_refs"] == ["late-reply-counter-evidence"]


def test_root_uses_governance_not_anonymous_address_disputes(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4", "user", effective_level="trusted")
    victim_wallet = _official_hot_wallet(service, 3)
    attacker_wallet = _official_hot_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="root-dispute-block-victim-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = service.submit_wallet_transaction(
        actor=victim,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=25,
        fee_points=1,
        request_uuid="root-dispute-block-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    tx = service.explorer_transaction(transfer["transaction_hash"])["transaction"]

    with pytest.raises(PermissionError, match="root 帳號不使用匿名地址疑義流程"):
        service.create_transaction_dispute(
            actor=root,
            tx_hash=transfer["transaction_hash"],
            statement="Root should handle official incidents through governance instead.",
            victim_wallet_address=victim_wallet["address"],
            claimed_amount_points=25,
            loss_cause="unknown",
            evidence=["root-dispute-block-open"],
            signature_nonce="root-dispute-block-open",
            from_wallet_address=victim_wallet["address"],
            to_wallet_address=attacker_wallet["address"],
            chain_branch=tx["chain_branch"],
            account_bound_proof=True,
        )

    dispute = service.create_transaction_dispute(
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        statement="Victim uses normal account-bound address proof for this dispute.",
        victim_wallet_address=victim_wallet["address"],
        claimed_amount_points=25,
        loss_cause="unknown",
        evidence=["root-dispute-block-victim-open"],
        signature_nonce="root-dispute-block-victim-open",
        from_wallet_address=victim_wallet["address"],
        to_wallet_address=attacker_wallet["address"],
        chain_branch=tx["chain_branch"],
        account_bound_proof=True,
    )["dispute"]

    with pytest.raises(PermissionError, match="root 帳號不使用匿名地址疑義回覆流程"):
        service.reply_transaction_dispute(
            actor=root,
            dispute_uuid=dispute["dispute_uuid"],
            statement="Root should not reply through anonymous address dispute either.",
            evidence=["root-dispute-block-reply"],
            signature_nonce="root-dispute-block-reply",
            account_bound_proof=True,
        )


def test_address_dispute_reply_requires_to_signature_and_hides_identity_terms(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4", "user", effective_level="trusted")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    attacker_wallet, attacker_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="address-dispute-reply-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=attacker_wallet["address"],
        amount=25,
        fee=1,
        request_uuid="address-dispute-reply-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    service.explorer_transaction(transfer["transaction_hash"])
    dispute = _address_signed_dispute(
        service,
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        from_wallet=victim_wallet,
        from_key=victim_key,
        to_wallet_address=attacker_wallet["address"],
        amount=25,
        statement="Unauthorized transfer claim opened for reply verification.",
        evidence=["reply-evidence-open"],
    )["dispute"]

    with pytest.raises(ValueError, match="public key does not match signer address"):
        _address_signed_dispute_reply(
            service,
            actor=attacker,
            dispute=dispute,
            to_key=victim_key,
            statement="This is a signed reply with the wrong wallet key.",
            evidence=["wrong-key"],
            nonce="reply-wrong-key",
        )
    with pytest.raises(ValueError, match="identity fields"):
        _address_signed_dispute_reply(
            service,
            actor=attacker,
            dispute=dispute,
            to_key=attacker_key,
            statement="My username is user4 and this should be rejected.",
            evidence=["identity-leak"],
            nonce="reply-identity-leak",
        )

    replied = _address_signed_dispute_reply(
        service,
        actor=attacker,
        dispute=dispute,
        to_key=attacker_key,
        statement="The transfer was payment for an external settlement reference.",
        evidence=["reply-valid-evidence"],
        nonce="reply-valid-nonce",
    )["dispute"]
    assert replied["reply_signature_verified"] is True
    assert replied["reply_statement"]
    assert "reporter_user_id" not in replied
    assert "reporter_username" not in replied
    _assert_address_dispute_payload_is_deidentified(replied)


def test_address_dispute_rejects_signed_payload_and_signature_replay(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4", "user", effective_level="trusted")
    root = _actor(1, "root", "super_admin")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    attacker_wallet, attacker_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=120,
        request_uuid="address-dispute-replay-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=attacker_wallet["address"],
        amount=25,
        fee=1,
        request_uuid="address-dispute-replay-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    service.explorer_transaction(transfer["transaction_hash"])

    dispute = _address_signed_dispute(
        service,
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        from_wallet=victim_wallet,
        from_key=victim_key,
        to_wallet_address=attacker_wallet["address"],
        amount=25,
        statement="Replay protected dispute opened with a fixed signed payload.",
        evidence=["replay-open"],
        nonce="open-replay-nonce",
    )["dispute"]
    service.review_transaction_dispute(
        actor=root,
        dispute_uuid=dispute["dispute_uuid"],
        status="rejected",
        review_note="close first case before replay attempt",
    )
    with pytest.raises(ValueError, match="replay rejected"):
        _address_signed_dispute(
            service,
            actor=victim,
            tx_hash=transfer["transaction_hash"],
            from_wallet=victim_wallet,
            from_key=victim_key,
            to_wallet_address=attacker_wallet["address"],
            amount=25,
            statement="Replay protected dispute opened with a fixed signed payload.",
            evidence=["replay-open"],
            nonce="open-replay-nonce",
        )

    second_transfer = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=attacker_wallet["address"],
        amount=10,
        fee=1,
        request_uuid="address-dispute-replay-reply-transfer",
    )
    _force_request_proved(service, second_transfer["transaction_hash"])
    service.explorer_transaction(second_transfer["transaction_hash"])
    second_dispute = _address_signed_dispute(
        service,
        actor=victim,
        tx_hash=second_transfer["transaction_hash"],
        from_wallet=victim_wallet,
        from_key=victim_key,
        to_wallet_address=attacker_wallet["address"],
        amount=10,
        statement="Replay protected dispute for the reply signature path.",
        evidence=["replay-reply"],
        nonce="open-replay-reply-case",
    )["dispute"]
    _address_signed_dispute_reply(
        service,
        actor=attacker,
        dispute=second_dispute,
        to_key=attacker_key,
        statement="This signed reply is intentionally submitted once only.",
        evidence=["reply-replay-once"],
        nonce="reply-replay-nonce",
    )
    with pytest.raises(ValueError, match="reply already exists"):
        _address_signed_dispute_reply(
            service,
            actor=attacker,
            dispute=second_dispute,
            to_key=attacker_key,
            statement="This signed reply is intentionally submitted once only.",
            evidence=["reply-replay-once"],
            nonce="reply-replay-nonce",
        )


def test_address_dispute_signature_purpose_and_mode_branch_are_bound(tmp_path):
    service = _service(tmp_path, user_count=10, mode="dev_ready")
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4", "user", effective_level="trusted")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    attacker_wallet, attacker_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=120,
        request_uuid="address-dispute-purpose-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=attacker_wallet["address"],
        amount=25,
        fee=1,
        request_uuid="address-dispute-purpose-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    tx = service.explorer_transaction(transfer["transaction_hash"])["transaction"]
    statement = "Purpose and branch binding prevents signature reuse."
    evidence = ["purpose-branch-proof"]
    evidence_hash = sha256_text(canonical_json(evidence))
    statement_hash = sha256_text(statement)

    wrong_purpose_payload = address_dispute_payload(
        tx_hash=transfer["transaction_hash"],
        from_wallet_address=victim_wallet["address"],
        to_wallet_address=attacker_wallet["address"],
        amount_points=25,
        statement_hash=statement_hash,
        evidence_hash=evidence_hash,
        nonce="wrong-purpose-open",
        chain_branch=tx["chain_branch"],
        purpose="address_dispute_reply",
        runtime_mode=service._address_dispute_runtime_mode(),
    )
    with pytest.raises(ValueError, match="signature verification failed"):
        service.create_transaction_dispute(
            actor=victim,
            tx_hash=transfer["transaction_hash"],
            statement=statement,
            victim_wallet_address=victim_wallet["address"],
            claimed_amount_points=25,
            loss_cause="private_key_leak",
            evidence=evidence,
            public_key_jwk=_public_jwk(victim_key),
            signature=_signature(victim_key, wrong_purpose_payload),
            signature_nonce="wrong-purpose-open",
            from_wallet_address=victim_wallet["address"],
            to_wallet_address=attacker_wallet["address"],
            chain_branch=tx["chain_branch"],
        )

    wrong_mode_payload = address_dispute_payload(
        tx_hash=transfer["transaction_hash"],
        from_wallet_address=victim_wallet["address"],
        to_wallet_address=attacker_wallet["address"],
        amount_points=25,
        statement_hash=statement_hash,
        evidence_hash=evidence_hash,
        nonce="wrong-mode-open",
        chain_branch=tx["chain_branch"],
        purpose="address_dispute_open",
        runtime_mode="production",
    )
    with pytest.raises(ValueError, match="signature verification failed"):
        service.create_transaction_dispute(
            actor=victim,
            tx_hash=transfer["transaction_hash"],
            statement=statement,
            victim_wallet_address=victim_wallet["address"],
            claimed_amount_points=25,
            loss_cause="private_key_leak",
            evidence=evidence,
            public_key_jwk=_public_jwk(victim_key),
            signature=_signature(victim_key, wrong_mode_payload),
            signature_nonce="wrong-mode-open",
            from_wallet_address=victim_wallet["address"],
            to_wallet_address=attacker_wallet["address"],
            chain_branch=tx["chain_branch"],
        )

    for wrong_branch in ("test", "internal", "superweak", "production"):
        wrong_branch_payload = address_dispute_payload(
            tx_hash=transfer["transaction_hash"],
            from_wallet_address=victim_wallet["address"],
            to_wallet_address=attacker_wallet["address"],
            amount_points=25,
            statement_hash=statement_hash,
            evidence_hash=evidence_hash,
            nonce=f"wrong-branch-{wrong_branch}",
            chain_branch=wrong_branch,
            purpose="address_dispute_open",
            runtime_mode=service._address_dispute_runtime_mode(),
        )
        with pytest.raises(ValueError, match="chain branch must match transaction branch"):
            service.create_transaction_dispute(
                actor=victim,
                tx_hash=transfer["transaction_hash"],
                statement=statement,
                victim_wallet_address=victim_wallet["address"],
                claimed_amount_points=25,
                loss_cause="private_key_leak",
                evidence=evidence,
                public_key_jwk=_public_jwk(victim_key),
                signature=_signature(victim_key, wrong_branch_payload),
                signature_nonce=f"wrong-branch-{wrong_branch}",
                from_wallet_address=victim_wallet["address"],
                to_wallet_address=attacker_wallet["address"],
                chain_branch=wrong_branch,
            )

    dispute = _address_signed_dispute(
        service,
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        from_wallet=victim_wallet,
        from_key=victim_key,
        to_wallet_address=attacker_wallet["address"],
        amount=25,
        statement=statement,
        evidence=evidence,
        nonce="valid-purpose-open",
    )["dispute"]
    wrong_reply_payload = address_dispute_payload(
        tx_hash=dispute["tx_hash"],
        from_wallet_address=dispute["from_wallet_address"],
        to_wallet_address=dispute["to_wallet_address"],
        amount_points=dispute["claimed_amount_points"],
        statement_hash=sha256_text("Purpose mixed reply must be rejected."),
        evidence_hash=sha256_text(canonical_json(["reply-purpose-mix"])),
        nonce="wrong-purpose-reply",
        chain_branch=dispute["chain_branch"],
        purpose="address_dispute_open",
        runtime_mode=dispute["signature_runtime_mode"],
    )
    with pytest.raises(ValueError, match="signature verification failed"):
        service.reply_transaction_dispute(
            actor=attacker,
            dispute_uuid=dispute["dispute_uuid"],
            statement="Purpose mixed reply must be rejected.",
            evidence=["reply-purpose-mix"],
            public_key_jwk=_public_jwk(attacker_key),
            signature=_signature(attacker_key, wrong_reply_payload),
            signature_nonce="wrong-purpose-reply",
        )


def test_address_dispute_provisional_freeze_is_outbound_only_for_to_address(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4", "user", effective_level="trusted")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    attacker_wallet, _attacker_key = _self_custody_wallet(service, 4)
    attacker_other_wallet, attacker_other_key = _self_custody_wallet(service, 4)
    for request_uuid, wallet, amount in (
        ("freeze-precision-victim-grant", victim_wallet, 150),
        ("freeze-precision-other-grant", attacker_other_wallet, 20),
    ):
        grant = _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=wallet["address"],
            amount=amount,
            request_uuid=request_uuid,
        )
        _force_request_proved(service, grant["transaction_hash"])
        service.explorer_transaction(grant["transaction_hash"])
    transfer = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=attacker_wallet["address"],
        amount=25,
        fee=1,
        request_uuid="freeze-precision-stolen-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    service.explorer_transaction(transfer["transaction_hash"])
    to_before = service.explorer_wallet(attacker_wallet["address"])["wallet"]

    dispute = _address_signed_dispute(
        service,
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        from_wallet=victim_wallet,
        from_key=victim_key,
        to_wallet_address=attacker_wallet["address"],
        amount=25,
        statement="Freeze precision dispute should only block the To address outbound path.",
        evidence=["freeze-precision"],
    )["dispute"]
    to_after = service.explorer_wallet(attacker_wallet["address"])["wallet"]
    assert to_after["points_balance"] == to_before["points_balance"]
    assert to_after["points_frozen"] == to_before["points_frozen"] == 0
    assert to_after["governance_freeze"]["source_dispute_uuid"] == dispute["dispute_uuid"]

    with pytest.raises(PermissionError, match="temporarily frozen pending governance review"):
        _signed_wallet_transfer(
            service,
            actor=attacker,
            source_wallet=attacker_wallet,
            source_key=_attacker_key,
            destination_wallet_address=victim_wallet["address"],
            amount=1,
            fee=0,
            request_uuid="freeze-precision-to-outbound-blocked",
        )

    other_transfer = _signed_wallet_transfer(
        service,
        actor=attacker,
        source_wallet=attacker_other_wallet,
        source_key=attacker_other_key,
        destination_wallet_address=victim_wallet["address"],
        amount=1,
        fee=0,
        request_uuid="freeze-precision-other-address-allowed",
    )
    assert other_transfer["transaction"]["status"] == "pending"
    other_view = service.explorer_wallet(attacker_other_wallet["address"])["wallet"]
    assert other_view["governance_freeze"] is None


def test_address_dispute_escalation_extends_freeze_and_expired_vote_releases(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    root = _actor(1, "root", "super_admin")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    attacker_wallet, _attacker_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="address-dispute-expiry-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=attacker_wallet["address"],
        amount=25,
        fee=1,
        request_uuid="address-dispute-expiry-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    service.explorer_transaction(transfer["transaction_hash"])
    dispute = _address_signed_dispute(
        service,
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        from_wallet=victim_wallet,
        from_key=victim_key,
        to_wallet_address=attacker_wallet["address"],
        amount=25,
        statement="Unauthorized transfer claim for freeze expiry verification.",
        evidence=["expiry-evidence"],
    )["dispute"]

    reviewed = service.review_transaction_dispute(
        actor=root,
        dispute_uuid=dispute["dispute_uuid"],
        status="approved",
        review_note="escalate to governance before temporary hold expires",
        recommended_strategy="tainted_remainder_return",
        create_proposal=True,
    )
    assert reviewed["provisional_freeze"]["wallet_address"] == attacker_wallet["address"]
    assert reviewed["dispute"]["escalated_freeze_expires_at"]
    freeze_uuid = reviewed["address_freeze_proposal"]["proposal_uuid"]
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute(
            "UPDATE points_chain_governance_proposals SET expires_at='2026-01-01T00:00:00Z' WHERE proposal_uuid=?",
            (freeze_uuid,),
        )
        service._refresh_governance_proposal_locked(conn, freeze_uuid)
        conn.commit()
    finally:
        conn.close()
    assert service.explorer_wallet(attacker_wallet["address"])["wallet"]["governance_freeze"] is None


def test_tainted_cold_wallet_funds_cannot_bypass_pc0_only_trading(tmp_path):
    service = _service(tmp_path, user_count=10)
    trading = _trading_service(service)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        ensure_trading_schema(conn)
        conn.commit()
    finally:
        conn.close()
    root = _actor(1, "root", "super_admin")
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4", "user", effective_level="trusted")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    attacker_wallet, _attacker_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=3000,
        request_uuid="tainted-trading-victim-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])
    transfer = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=attacker_wallet["address"],
        amount=2500,
        fee=1,
        request_uuid="tainted-trading-theft-transfer",
    )
    _force_request_proved(service, transfer["transaction_hash"])
    service.explorer_transaction(transfer["transaction_hash"])

    with pytest.raises(ValueError, match="交易所僅支援 pc0"):
        trading.place_order(
            actor=attacker,
            market_symbol="BTC/POINTS",
            side="buy",
            order_type="market",
            quantity="0.02",
            source_wallet_address=attacker_wallet["address"],
        )

    dispute = _address_signed_dispute(
        service,
        actor=victim,
        tx_hash=transfer["transaction_hash"],
        from_wallet=victim_wallet,
        from_key=victim_key,
        to_wallet_address=attacker_wallet["address"],
        amount=2500,
        statement="Unauthorized transfer after private key phishing during test.",
        evidence=[transfer["transaction_hash"]],
    )["dispute"]
    reviewed = service.review_transaction_dispute(
        actor=root,
        dispute_uuid=dispute["dispute_uuid"],
        status="approved",
        review_note="review after attacker bought spot and deleted the funding wallet",
        recommended_strategy="tainted_remainder_return",
        create_proposal=True,
    )

    assert reviewed["provisional_freeze"]["wallet_address"] == attacker_wallet["address"]
    assert reviewed["taint_exposure"]["summary"]["spot_position_count"] == 0
    assert reviewed["proposal"]["payload"]["product_exposure"]["summary"]["spot_position_count"] == 0

    conn = service.get_db()
    try:
        position_count = conn.execute(
            "SELECT COUNT(*) FROM trading_spot_positions WHERE user_id=4 AND market_symbol='BTC/POINTS'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert position_count == 0


def test_recovery_branch_can_strictly_exclude_tainted_descendants(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    manager = _actor(2, "user2", "manager")
    victim = _actor(3, "user3", "user", effective_level="trusted")
    attacker = _actor(4, "user4")
    victim_wallet = _official_hot_wallet(service, 3)
    attacker_wallet = _official_hot_wallet(service, 4)
    innocent_wallet = _official_hot_wallet(service, 7)

    for grant in (
        _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=victim_wallet["address"],
            amount=100,
            request_uuid="strict-branch-victim-grant",
        ),
        _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=attacker_wallet["address"],
            amount=5,
            request_uuid="strict-branch-attacker-legit-grant",
        ),
    ):
        _force_request_proved(service, grant["transaction_hash"])
        service.explorer_transaction(grant["transaction_hash"])

    stolen = service.submit_wallet_transaction(
        actor=victim,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=attacker_wallet["address"],
        amount_points=40,
        fee_points=1,
        request_uuid="strict-branch-stolen-transfer",
    )
    _force_request_proved(service, stolen["transaction_hash"])
    service.explorer_transaction(stolen["transaction_hash"])
    tainted_spend = service.submit_wallet_transaction(
        actor=attacker,
        source_wallet_address=attacker_wallet["address"],
        destination_wallet_address=innocent_wallet["address"],
        amount_points=30,
        fee_points=1,
        request_uuid="strict-branch-tainted-spend",
    )
    _force_request_proved(service, tainted_spend["transaction_hash"])
    service.explorer_transaction(tainted_spend["transaction_hash"])

    created = service.create_emergency_recovery_branch_proposal(
        actor=root,
        incident_tx_hash=stolen["transaction_hash"],
        reason="strict hard-fork recovery excludes dependent tainted descendants",
        excluded_tx_hashes=[stolen["transaction_hash"]],
        recovery_strategy="exclude_tainted_descendants",
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    service.cast_governance_vote(actor=root, proposal_uuid=proposal_uuid, vote="yes")
    service.cast_governance_vote(actor=manager, proposal_uuid=proposal_uuid, vote="yes")
    seed = service.execute_governance_proposal(actor=manager, proposal_uuid=proposal_uuid)["result"]["recovery_seed"]

    assert tainted_spend["transaction_hash"] in seed["auto_excluded_refs"]
    assert service.explorer_wallet(victim_wallet["address"])["wallet"]["points_balance"] == 100
    assert service.explorer_wallet(attacker_wallet["address"])["wallet"]["points_balance"] == 5
    assert service.explorer_wallet(innocent_wallet["address"])["wallet"]["points_balance"] == 0


def test_cold_wallet_signatures_are_branch_action_and_signer_bound(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    destination, _destination_key = _self_custody_wallet(service, 4)

    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="cold-replay-victim-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])

    old_branch_payload = wallet_transaction_payload(
        user_id=3,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=destination["address"],
        amount_points=10,
        fee_points=1,
        request_uuid="cold-cross-branch-replay",
        memo="signed on main",
        chain_branch="main",
        signer_key_id=victim_wallet["public_key_hash"],
    )
    old_branch_signature = _signature(victim_key, old_branch_payload)
    with pytest.raises(ValueError, match="signature verification failed"):
        service.submit_wallet_transaction(
            actor=victim,
            source_wallet_address=victim_wallet["address"],
            destination_wallet_address=destination["address"],
            amount_points=10,
            fee_points=1,
            request_uuid="cold-cross-request-replay",
            memo="signed on main",
            signature=old_branch_signature,
        )
    service_fee_payload = wallet_service_fee_payload(
        user_id=3,
        source_wallet_address=victim_wallet["address"],
        item_key="comfyui_txt2img_basic",
        quantity=1,
        amount_points=5,
        request_uuid="cold-action-replay-service-fee",
        reference_type="price_catalog",
        reference_id="comfyui_txt2img_basic",
        chain_branch="main",
        signer_key_id=victim_wallet["public_key_hash"],
    )
    service_fee_signature = _signature(victim_key, service_fee_payload)

    branch = _activate_recovery_branch(service, incident_tx_hash="cold-replay-incident", excluded_tx_hashes=[])
    assert branch["branch_uuid"] != "main"

    with pytest.raises(ValueError, match="signature verification failed"):
        service.submit_wallet_transaction(
            actor=victim,
            source_wallet_address=victim_wallet["address"],
            destination_wallet_address=destination["address"],
            amount_points=10,
            fee_points=1,
            request_uuid="cold-cross-branch-replay",
            memo="signed on main",
            signature=old_branch_signature,
        )
    with pytest.raises(ValueError, match="signature verification failed"):
        service.submit_wallet_transaction(
            actor=victim,
            source_wallet_address=victim_wallet["address"],
            destination_wallet_address=destination["address"],
            amount_points=5,
            fee_points=1,
            request_uuid="cold-action-replay-service-fee",
            memo="try service fee signature as transfer",
            signature=service_fee_signature,
        )


def test_service_fee_charge_is_branch_scoped_and_old_branch_charge_cannot_replay(tmp_path):
    service = _service(tmp_path, user_count=10)
    user = _actor(3, "user3", "user", effective_level="trusted")
    source = _official_hot_wallet(service, 3)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=source["address"],
        amount=100,
        request_uuid="service-fee-branch-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])

    charge = service.spend_points(
        user_id=3,
        item_key="comfyui_txt2img_basic",
        source_wallet_address=source["address"],
        request_uuid="service-fee-old-branch-request",
        idempotency_key="service-fee-old-branch-idem",
        actor=user,
    )
    assert charge["charge"]["chain_branch"] == "main"
    assert charge["charge"]["status"] == "settled"
    assert charge["settlement_policy"] == "internal_hot_wallet_immediate_debit"

    branch = _activate_recovery_branch(service, incident_tx_hash="service-fee-branch-incident", excluded_tx_hashes=[])
    with pytest.raises(ValueError, match="non-canonical branch"):
        service.spend_points(
            user_id=3,
            item_key="comfyui_txt2img_basic",
            source_wallet_address=source["address"],
            request_uuid="service-fee-old-branch-request",
            idempotency_key="service-fee-old-branch-idem",
            actor=user,
        )
    new_charge = service.spend_points(
        user_id=3,
        item_key="comfyui_txt2img_basic",
        source_wallet_address=source["address"],
        request_uuid="service-fee-new-branch-request",
        idempotency_key="service-fee-new-branch-idem",
        actor=user,
    )
    assert new_charge["charge"]["chain_branch"] == branch["branch_uuid"]
    assert new_charge["charge"]["status"] == "settled"


def test_official_funds_are_branch_scoped_after_recovery_branch(tmp_path):
    service = _service(tmp_path, user_count=10)
    before = service.economy_stats()["economy_layer"]
    assert before["chain_branch"] == "main"
    assert before["funds"]["official_treasury"]["balance"] > 0

    branch = _activate_recovery_branch(service, incident_tx_hash="fund-branch-incident", excluded_tx_hashes=[])
    after = service.economy_stats()["economy_layer"]
    assert after["chain_branch"] == branch["branch_uuid"]
    assert after["funds"]["official_treasury"]["balance"] == before["funds"]["official_treasury"]["balance"]
    assert after["funds"]["promo_fund"]["balance"] == before["funds"]["promo_fund"]["balance"]
    assert after["funds"]["exchange_fund"]["balance"] == before["funds"]["exchange_fund"]["balance"]
    assert after["funds"]["burn"]["balance"] == before["funds"]["burn"]["balance"]
    assert after["health"]["status"] == before["health"]["status"]
    assert branch["recovery_seed"]["system_funds"]["created_count"] >= 3

    strict_service = _service(tmp_path / "strict-funds", user_count=10)
    strict_before = strict_service.economy_stats()["economy_layer"]
    strict_branch = _activate_recovery_branch(
        strict_service,
        incident_tx_hash="fund-branch-strict-incident",
        excluded_tx_hashes=[],
        recovery_strategy="exclude_tainted_descendants",
    )
    strict_after = strict_service.economy_stats()["economy_layer"]
    assert strict_after["chain_branch"] == strict_branch["branch_uuid"]
    assert strict_after["funds"]["official_treasury"]["balance"] == strict_before["funds"]["official_treasury"]["balance"]
    assert strict_after["funds"]["promo_fund"]["balance"] == strict_before["funds"]["promo_fund"]["balance"]
    assert strict_after["funds"]["exchange_fund"]["balance"] == strict_before["funds"]["exchange_fund"]["balance"]


def test_transactions_list_marks_pc0_official_source_transfer_confirmed(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    destination = _official_hot_wallet(service, 3)
    pending = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=destination["address"],
        amount=25,
        request_uuid="list-official-source-insufficient",
    )

    pending_request_uuid = pending["transaction"]["input_data"]["request_uuid"]

    listed = service.list_wallet_transactions(user_id=1, actor=root, limit=100)
    target = next(item for item in listed["transactions"] if item["request_uuid"] == pending_request_uuid)
    assert target["status"] == "confirmed"
    assert target["settlement_rail"] == "internal_hot_wallet"
    assert target["network_fee_points"] == 0
    assert listed["summary"]["finalization_error_count"] == 0
    assert listed.get("warnings", []) == []
    assert service.explorer_wallet(destination["address"])["wallet"]["points_balance"] == 25
    assert service.explorer_transaction(pending["transaction_hash"])["transaction"]["status"] == "confirmed"


def test_multiple_recovery_forks_preserve_canonical_ledger_and_do_not_zero_funds(tmp_path):
    service = _service(tmp_path, user_count=12)
    victims = [_actor(3, "user3", "user"), _actor(4, "user4", "user"), _actor(5, "user5", "user")]
    attackers = [_actor(6, "user6", "user"), _actor(7, "user7", "user"), _actor(8, "user8", "user")]
    normal_a = _actor(9, "user9", "user")
    normal_b = _actor(10, "user10", "user")
    victim_wallets = [_official_hot_wallet(service, actor["id"]) for actor in victims]
    attacker_wallets = [_official_hot_wallet(service, actor["id"]) for actor in attackers]
    normal_a_wallet = _official_hot_wallet(service, normal_a["id"])
    normal_b_wallet = _official_hot_wallet(service, normal_b["id"])

    grants = []
    for index, wallet in enumerate(victim_wallets, start=1):
        grants.append(_official_treasury_grant_via_governance(
            service,
            destination_wallet_address=wallet["address"],
            amount=100,
            request_uuid=f"multi-fork-victim-{index}-grant",
        ))
    grants.extend([
        _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=normal_a_wallet["address"],
            amount=50,
            request_uuid="multi-fork-normal-a-grant",
        ),
        _official_treasury_grant_via_governance(
            service,
            destination_wallet_address=normal_b_wallet["address"],
            amount=10,
            request_uuid="multi-fork-normal-b-grant",
        ),
    ])
    for grant in grants:
        _force_request_proved(service, grant["transaction_hash"])
        service.explorer_transaction(grant["transaction_hash"])

    def assert_chain_invariants(expected_branch, *, previous_branches):
        verify = service.verify_chain()
        assert verify["ok"] is True
        stats = service.economy_stats(verification=verify)["economy_layer"]
        assert stats["chain_branch"] == expected_branch
        assert stats["replay"]["derived_verify"]["ok"] is True
        assert stats["supply_equation"]["actual_supply_equation_gap_points"] == 0
        assert stats["supply_equation"]["bridged_supply_equation_gap_points"] == 0
        assert stats["supply_equation"]["bridged_supply_equation_balanced"] is True
        assert stats["funds"]["official_treasury"]["balance"] > 0
        assert stats["funds"]["promo_fund"]["balance"] > 0
        assert stats["funds"]["exchange_fund"]["balance"] > 0
        assert service.explorer_wallet(economy_fund_address(service.chain_secret, "mint"))["wallet"]["points_balance"] > 0
        assert service.explorer_wallet(economy_fund_address(service.chain_secret, "official_treasury"))["wallet"]["points_balance"] > 0
        assert service.explorer_wallet(economy_fund_address(service.chain_secret, "promo_fund"))["wallet"]["points_balance"] > 0
        assert service.explorer_wallet(economy_fund_address(service.chain_secret, "exchange_fund"))["wallet"]["points_balance"] > 0
        report = service.root_report()
        branches = report["governance"]["branches"]
        branch_tree = report["governance"]["branch_tree"]
        assert branch_tree["canonical_branch_uuid"] == expected_branch
        assert branch_tree["write_enabled_count"] == 1
        tree_nodes = {item["branch_uuid"]: item for item in branch_tree["nodes"]}
        assert expected_branch in tree_nodes
        assert tree_nodes[expected_branch]["is_canonical"] is True
        assert tree_nodes[expected_branch]["ledger_count"] >= 0
        canonical = [item for item in branches if item.get("is_canonical")]
        if expected_branch == "main" and not branches:
            canonical = [{"branch_uuid": "main"}]
        assert [item["branch_uuid"] for item in canonical] == [expected_branch]
        archived = {item["branch_uuid"]: item for item in branches if item["branch_uuid"] in previous_branches}
        for branch_uuid in previous_branches:
            assert archived[branch_uuid]["write_enabled"] is False
            assert tree_nodes[branch_uuid]["is_canonical"] is False
            assert tree_nodes[branch_uuid]["auto_collapsed"] is True

    assert_chain_invariants("main", previous_branches=[])

    old_request_uuids = []
    old_tx_hashes = []
    expected_victim_balances = [100, 100, 100]
    expected_normal = {
        normal_a_wallet["address"]: 50,
        normal_b_wallet["address"]: 10,
    }
    previous_branches = ["main"]
    active_branch = "main"

    for round_index, amount in enumerate([25, 20, 15], start=1):
        if round_index == 2:
            normal = service.submit_wallet_transaction(
                actor=normal_a,
                source_wallet_address=normal_a_wallet["address"],
                destination_wallet_address=normal_b_wallet["address"],
                amount_points=7,
                fee_points=1,
                request_uuid="multi-fork-normal-transfer-1",
            )
            _force_request_proved(service, normal["transaction_hash"])
            service.explorer_transaction(normal["transaction_hash"])
            old_request_uuids.append("multi-fork-normal-transfer-1")
            old_tx_hashes.append(normal["transaction_hash"])
            expected_normal[normal_a_wallet["address"]] -= 7
            expected_normal[normal_b_wallet["address"]] += 7
        if round_index == 3:
            normal = service.submit_wallet_transaction(
                actor=normal_b,
                source_wallet_address=normal_b_wallet["address"],
                destination_wallet_address=normal_a_wallet["address"],
                amount_points=2,
                fee_points=1,
                request_uuid="multi-fork-normal-transfer-2",
            )
            _force_request_proved(service, normal["transaction_hash"])
            service.explorer_transaction(normal["transaction_hash"])
            old_request_uuids.append("multi-fork-normal-transfer-2")
            old_tx_hashes.append(normal["transaction_hash"])
            expected_normal[normal_b_wallet["address"]] -= 2
            expected_normal[normal_a_wallet["address"]] += 2

        victim_index = round_index - 1
        theft = service.submit_wallet_transaction(
            actor=victims[victim_index],
            source_wallet_address=victim_wallets[victim_index]["address"],
            destination_wallet_address=attacker_wallets[victim_index]["address"],
            amount_points=amount,
            fee_points=1,
            request_uuid=f"multi-fork-theft-{round_index}",
            memo=f"multi fork compromised transfer {round_index}",
        )
        _force_request_proved(service, theft["transaction_hash"])
        service.explorer_transaction(theft["transaction_hash"])
        old_request_uuids.append(f"multi-fork-theft-{round_index}")
        old_tx_hashes.append(theft["transaction_hash"])

        branch = _activate_tainted_remainder_recovery(
            service,
            incident_tx_hash=theft["transaction_hash"],
            victim_wallet_address=victim_wallets[victim_index]["address"],
            claim_amount=amount,
            claim_id=f"multi-fork-claim-{round_index}",
        )
        assert branch["parent_branch_uuid"] == active_branch
        active_branch = branch["branch_uuid"]
        assert_chain_invariants(active_branch, previous_branches=previous_branches)
        previous_branches.append(active_branch)

        assert branch["recovery_seed"]["tainted_remainder_return"]["return_amount"] == amount
        for idx, wallet in enumerate(victim_wallets):
            wallet_after = service.explorer_wallet(wallet["address"])["wallet"]
            assert wallet_after["chain_branch"] == active_branch
            assert wallet_after["points_balance"] == expected_victim_balances[idx]
        for wallet in attacker_wallets:
            wallet_after = service.explorer_wallet(wallet["address"])["wallet"]
            assert wallet_after["chain_branch"] == active_branch
            assert wallet_after["points_balance"] == 0
        assert service.explorer_wallet(normal_a_wallet["address"])["wallet"]["points_balance"] == expected_normal[normal_a_wallet["address"]]
        assert service.explorer_wallet(normal_b_wallet["address"])["wallet"]["points_balance"] == expected_normal[normal_b_wallet["address"]]
        assert service.explorer_transaction(theft["transaction_hash"])["transaction"]["branch"]["is_canonical"] is False
        with pytest.raises(ValueError, match="non-canonical branch"):
            service.submit_wallet_transaction(
                actor=victims[victim_index],
                source_wallet_address=victim_wallets[victim_index]["address"],
                destination_wallet_address=attacker_wallets[victim_index]["address"],
                amount_points=amount,
                fee_points=1,
                request_uuid=f"multi-fork-theft-{round_index}",
            )

    for tx_hash in old_tx_hashes:
        tx = service.explorer_transaction(tx_hash)["transaction"]
        assert tx["chain_branch"] != active_branch
        assert tx["branch"]["is_canonical"] is False
        assert tx["branch"]["write_enabled"] is False


def test_root_report_filters_expired_provisional_freeze_entries(tmp_path):
    service = _service(tmp_path, user_count=4)
    wallet = _official_hot_wallet(service, 3)
    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO points_chain_address_provisional_freezes (
                wallet_address, status, reason, evidence_json, source_dispute_uuid,
                linked_proposal_uuid, reviewed_by, created_at, updated_at, expires_at
            ) VALUES (?, 'active', 'expired test freeze', '[]', 'pcdispute:expired',
                      'pcgov:expired', 1, '2026-01-01T00:00:00Z',
                      '2026-01-01T00:00:00Z', '2026-01-01T01:00:00Z')
            """,
            (wallet["address"],),
        )
        conn.commit()
    finally:
        conn.close()

    report = service.root_report()

    assert all(item is not None for item in report["governance"]["active_provisional_freezes"])
    assert not any(
        item.get("wallet_address") == wallet["address"]
        for item in report["governance"]["active_provisional_freezes"]
    )
    conn = service.get_db()
    try:
        status = conn.execute(
            "SELECT status FROM points_chain_address_provisional_freezes WHERE wallet_address=?",
            (wallet["address"],),
        ).fetchone()["status"]
    finally:
        conn.close()
    assert status == "expired"


def test_acceleration_cannot_bypass_pending_freeze_or_cross_branch(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    victim_wallet = _official_hot_wallet(service, 3)
    destination, _destination_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="acceleration-freeze-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])

    pending = service.submit_wallet_transaction(
        actor=victim,
        source_wallet_address=victim_wallet["address"],
        destination_wallet_address=destination["address"],
        amount_points=90,
        fee_points=1,
        request_uuid="acceleration-freeze-pending",
    )
    with pytest.raises(ValueError, match="insufficient balance"):
        service.accelerate_explorer_transaction(
            actor=victim,
            ledger_ref=pending["transaction_hash"],
            fee_points=20,
            request_uuid="acceleration-bypass-freeze",
        )

    branch = _activate_recovery_branch(service, incident_tx_hash="acceleration-branch-incident", excluded_tx_hashes=[])
    assert branch["branch_uuid"] != "main"
    with pytest.raises(PermissionError, match="non-canonical branch"):
        service.accelerate_explorer_transaction(
            actor=victim,
            ledger_ref=pending["transaction_hash"],
            fee_points=1,
            request_uuid="acceleration-old-branch",
        )


def test_acceleration_does_not_bypass_finality(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    victim_wallet, victim_key = _self_custody_wallet(service, 3)
    destination, _destination_key = _self_custody_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="acceleration-finality-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])

    pending = _signed_wallet_transfer(
        service,
        actor=victim,
        source_wallet=victim_wallet,
        source_key=victim_key,
        destination_wallet_address=destination["address"],
        amount=10,
        fee=1,
        request_uuid="acceleration-finality-pending",
    )
    accelerated = service.accelerate_explorer_transaction(
        actor=victim,
        ledger_ref=pending["transaction_hash"],
        fee_points=2,
        request_uuid="acceleration-finality-fee",
    )
    assert accelerated["created"] is True
    still_pending = service.explorer_transaction(pending["transaction_hash"])["transaction"]
    assert still_pending["status"] == "pending"
    assert service.explorer_wallet(destination["address"])["wallet"]["points_balance"] == 0

    _force_request_proved(service, pending["transaction_hash"])
    confirmed = service.explorer_transaction(pending["transaction_hash"])["transaction"]
    assert confirmed["status"] == "confirmed"
    assert service.explorer_wallet(destination["address"])["wallet"]["points_balance"] == 10


def test_concurrent_wallet_transfers_cannot_double_spend(tmp_path):
    service = _service(tmp_path, user_count=10)
    victim = _actor(3, "user3", "user", effective_level="trusted")
    victim_wallet = _official_hot_wallet(service, 3)
    destination = _official_hot_wallet(service, 4)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=victim_wallet["address"],
        amount=100,
        request_uuid="concurrent-double-spend-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])

    def send(index):
        try:
            return ("ok", service.submit_wallet_transaction(
                actor=victim,
                source_wallet_address=victim_wallet["address"],
                destination_wallet_address=destination["address"],
                amount_points=60,
                fee_points=1,
                request_uuid=f"concurrent-double-spend-{index}",
            ))
        except Exception as exc:
            return ("err", str(exc))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(2)))
    assert [kind for kind, _payload in results].count("ok") == 1
    assert any("insufficient balance" in payload for kind, payload in results if kind == "err")
    wallet = service.explorer_wallet(victim_wallet["address"])["wallet"]
    destination_wallet = service.explorer_wallet(destination["address"])["wallet"]
    assert wallet["points_balance"] == 40
    assert wallet["pending_outgoing_points"] == 0
    assert destination_wallet["points_balance"] == 60


def test_branch_recovery_safe_mode_does_not_offer_backup_restore(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    branch = _activate_recovery_branch(service, incident_tx_hash="restore-branch-incident", excluded_tx_hashes=[])
    backup = service.create_ledger_backup(reason="branch restore consistency", kind="manual")
    assert backup["disabled"] is True

    conn = service.get_db()
    try:
        service.ensure_schema(conn)
        service._enter_safe_mode(
            conn,
            {"ok": False, "errors": [{"type": "test_restore_lockdown", "severity": "critical"}]},
            "test_restore_lockdown",
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PermissionError, match="backup restore is disabled"):
        service.restore_from_backup(actor=root, backup_id="legacy", confirm="RESTORE POINTSCHAIN")
    report = service.root_report()
    canonical = [item for item in report["governance"]["branches"] if item["is_canonical"]]
    assert len(canonical) == 1
    assert canonical[0]["branch_uuid"] == branch["branch_uuid"]
    assert report["recovery"]["restore_plan"]["mode"] == "branch_governance_recovery"
    assert report["recovery"]["restore_plan"]["backup_restore_disabled"] is True
    assert service.verify_governance_audit()["ok"] is True


def test_governance_freeze_blocks_outgoing_without_mutating_ledger(tmp_path):
    service = _service(tmp_path, user_count=10)
    root = _actor(1, "root", "super_admin")
    source = _official_hot_wallet(service, 2)
    destination = _official_hot_wallet(service, 3)
    grant = _official_treasury_grant_via_governance(
        service,
        destination_wallet_address=source["address"],
        amount=100,
        request_uuid="governance-freeze-grant",
    )
    _force_request_proved(service, grant["transaction_hash"])
    service.explorer_transaction(grant["transaction_hash"])

    created = service.create_wallet_freeze_proposal(
        actor=root,
        wallet_address=source["address"],
        reason="suspected exploit outgoing freeze",
        evidence=[grant["transaction_hash"]],
    )
    proposal_uuid = created["proposal"]["proposal_uuid"]
    _vote_yes_until_passed(service, proposal_uuid, 5)
    executed = service.execute_governance_proposal(actor=root, proposal_uuid=proposal_uuid)
    assert executed["result"]["action"] == "wallet_address_frozen"
    assert executed["result"]["ledger_mutation"] == "forbidden"
    assert service.explorer_wallet(source["address"])["wallet"]["governance_freeze"]["status"] == "active"

    with pytest.raises(PermissionError, match="frozen by governance"):
        service.submit_wallet_transaction(
            actor=_actor(2, "user2"),
            source_wallet_address=source["address"],
            destination_wallet_address=destination["address"],
            amount_points=1,
            fee_points=0,
            request_uuid="governance-freeze-blocked-transfer",
        )

    released = service.create_wallet_freeze_proposal(
        actor=root,
        wallet_address=source["address"],
        reason="release after incident review",
        release=True,
    )
    release_uuid = released["proposal"]["proposal_uuid"]
    _vote_yes_until_passed(service, release_uuid, 5)
    service.execute_governance_proposal(actor=root, proposal_uuid=release_uuid)
    assert service.explorer_wallet(source["address"])["wallet"]["governance_freeze"] is None

    transfer = service.submit_wallet_transaction(
        actor=_actor(2, "user2"),
        source_wallet_address=source["address"],
        destination_wallet_address=destination["address"],
        amount_points=1,
        fee_points=0,
        request_uuid="governance-freeze-released-transfer",
    )
    assert transfer["created"] is True
