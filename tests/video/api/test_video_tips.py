import json
import sqlite3

import pytest

from services.points_chain import DISPLAY_CURRENCY, PointsLedgerService
from services.media.videos import publish_video, tip_video
from services.server.finance_database import get_finance_db
from tests.video.helpers.video_test_helpers import actor, seed_cloud_file, video_test_db


def _points_service(conn):
    service = PointsLedgerService(get_db=lambda: conn, chain_secret="video-test-secret")
    service.ensure_schema(conn)
    service._record_transaction(
        conn,
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="credit",
        amount=200,
        action_type="user_initial_grant",
        reference_type="test",
        reference_id="seed",
        idempotency_key="seed:viewer",
        reason="seed viewer",
        actor=actor(1, "owner"),
    )
    return service


def test_video_tip_debits_viewer_credits_uploader_and_is_idempotent():
    conn = video_test_db()
    seed_cloud_file(conn)
    video = publish_video(conn, actor=actor(1, "owner"), cloud_file_id="file-video", title="Demo")
    service = _points_service(conn)

    result = tip_video(
        conn,
        points_service=service,
        actor=actor(2, "viewer"),
        video_id=video["id"],
        amount=100,
        fee_percent=5,
        idempotency_key="tip-once",
    )
    assert result["created"] is True
    assert result["tip"]["amount_points"] == 100
    assert result["tip"]["fee_points"] == 5
    assert result["tip"]["net_points"] == 95
    assert result["tip"]["fee_user_id"] == 9
    assert result["ledger"]["fee_uuid"]

    again = tip_video(
        conn,
        points_service=service,
        actor=actor(2, "viewer"),
        video_id=video["id"],
        amount=100,
        fee_percent=5,
        idempotency_key="tip-once",
    )
    assert again["created"] is False

    wallet_viewer = conn.execute("SELECT soft_balance FROM points_wallets WHERE user_id=2").fetchone()[0]
    wallet_owner = conn.execute("SELECT soft_balance FROM points_wallets WHERE user_id=1").fetchone()[0]
    wallet_official = conn.execute("SELECT soft_balance FROM points_wallets WHERE user_id=9").fetchone()[0]
    assert wallet_viewer == 100
    assert wallet_owner == 95
    assert wallet_official == 5
    assert conn.execute("SELECT COUNT(*) FROM video_tips").fetchone()[0] == 1
    tip_events = conn.execute(
        """
        SELECT transaction_type, source_fund_key, source_address, destination_fund_key, destination_address, amount
        FROM points_economy_events
        WHERE transaction_type LIKE 'video_tip_%'
        ORDER BY id ASC
        """
    ).fetchall()
    assert [(row["transaction_type"], int(row["amount"])) for row in tip_events] == [
        ("video_tip_credit", 95),
        ("video_tip_platform_fee", 5),
    ]
    assert sum(int(row["amount"]) for row in tip_events) == 100
    assert tip_events[0]["destination_fund_key"] in {"", None}
    assert tip_events[1]["destination_fund_key"] == "official_treasury"


def test_root_video_tip_net_revenue_is_official_treasury_income():
    conn = video_test_db()
    seed_cloud_file(conn, owner_user_id=9)
    video = publish_video(conn, actor=actor(9, "root", "super_admin"), cloud_file_id="file-video", title="Official")
    service = _points_service(conn)

    result = tip_video(
        conn,
        points_service=service,
        actor=actor(2, "viewer"),
        video_id=video["id"],
        amount=100,
        fee_percent=5,
        idempotency_key="tip-root-video",
    )

    assert result["created"] is True
    assert result["tip"]["to_user_id"] == 9
    tip_events = conn.execute(
        """
        SELECT transaction_type, destination_fund_key, amount
        FROM points_economy_events
        WHERE transaction_type LIKE 'video_tip_%'
        ORDER BY id ASC
        """
    ).fetchall()
    assert [(row["transaction_type"], row["destination_fund_key"], int(row["amount"])) for row in tip_events] == [
        ("video_tip_credit", "official_treasury", 95),
        ("video_tip_platform_fee", "official_treasury", 5),
    ]
    assert sum(int(row["amount"]) for row in tip_events) == 100
    credit = conn.execute(
        "SELECT public_metadata_json FROM points_ledger WHERE ledger_uuid=?",
        (result["ledger"]["credit_uuid"],),
    ).fetchone()
    assert credit is not None
    metadata = json.loads(credit["public_metadata_json"])
    assert metadata["destination_fund_key"] == "official_treasury"
    assert metadata["wallet_flow_snapshot"]["destination_fund_key"] == "official_treasury"


def test_video_tip_rejects_idempotency_key_reuse_for_different_tip():
    conn = video_test_db()
    seed_cloud_file(conn)
    video = publish_video(conn, actor=actor(1, "owner"), cloud_file_id="file-video", title="Demo")
    service = _points_service(conn)

    tip_video(
        conn,
        points_service=service,
        actor=actor(2, "viewer"),
        video_id=video["id"],
        amount=25,
        fee_percent=5,
        idempotency_key="shared-key",
    )
    with pytest.raises(ValueError, match="idempotency key conflicts"):
        tip_video(
            conn,
            points_service=service,
            actor=actor(2, "viewer"),
            video_id=video["id"],
            amount=30,
            fee_percent=5,
            idempotency_key="shared-key",
        )


def test_video_tip_rejects_insufficient_balance_and_self_tip():
    conn = video_test_db()
    seed_cloud_file(conn)
    video = publish_video(conn, actor=actor(1, "owner"), cloud_file_id="file-video", title="Demo")
    service = _points_service(conn)

    with pytest.raises(ValueError, match="own video"):
        tip_video(conn, points_service=service, actor=actor(1, "owner"), video_id=video["id"], amount=1)

    with pytest.raises(ValueError, match="insufficient balance"):
        tip_video(conn, points_service=service, actor=actor(2, "viewer"), video_id=video["id"], amount=1000)


def test_video_tip_split_database_replays_finance_after_core_insert_failure(tmp_path):
    core_path = tmp_path / "database.db"
    finance_path = tmp_path / "finance.db"
    template = video_test_db()
    template.commit()
    core = sqlite3.connect(core_path)
    core.row_factory = sqlite3.Row
    template.backup(core)
    template.close()
    seed_cloud_file(core)
    video = publish_video(core, actor=actor(1, "owner"), cloud_file_id="file-video", title="Demo")
    core.commit()

    def finance_db():
        return get_finance_db(finance_path, core_db_path=core_path)

    service = PointsLedgerService(get_db=finance_db, chain_secret="video-split-test-secret")
    finance = finance_db()
    service.ensure_schema(finance)
    service._record_transaction(
        finance,
        user_id=2,
        currency_type=DISPLAY_CURRENCY,
        direction="credit",
        amount=200,
        action_type="user_initial_grant",
        reference_type="test",
        reference_id="seed",
        idempotency_key="seed:viewer",
        reason="seed viewer",
        actor=actor(1, "owner"),
    )
    finance.commit()
    finance.close()

    core.execute(
        """
        CREATE TRIGGER fail_first_video_tip
        BEFORE INSERT ON video_tips
        BEGIN
            SELECT RAISE(ABORT, 'simulated core write failure');
        END
        """
    )
    core.commit()
    finance = finance_db()
    finance.execute("BEGIN")
    with pytest.raises(sqlite3.IntegrityError, match="simulated core write failure"):
        tip_video(
            core,
            points_service=service,
            actor=actor(2, "viewer"),
            video_id=video["id"],
            amount=100,
            fee_percent=5,
            idempotency_key="split-tip-once",
            points_conn=finance,
            commit_points_before_record=True,
        )
    core.rollback()
    finance.close()

    core.execute("DROP TRIGGER fail_first_video_tip")
    core.commit()
    finance = finance_db()
    finance.execute("BEGIN")
    result = tip_video(
        core,
        points_service=service,
        actor=actor(2, "viewer"),
        video_id=video["id"],
        amount=100,
        fee_percent=5,
        idempotency_key="split-tip-once",
        points_conn=finance,
        commit_points_before_record=True,
    )
    core.commit()
    finance.close()

    assert result["created"] is True
    assert result["settlement_replayed"] is True
    assert core.execute("SELECT COUNT(*) FROM video_tips").fetchone()[0] == 1
    assert core.execute("SELECT coin_total FROM videos WHERE id=?", (video["id"],)).fetchone()[0] == 100
    assert core.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='points_wallets'").fetchone() is None

    finance = finance_db()
    assert finance.execute("SELECT soft_balance FROM points_wallets WHERE user_id=2").fetchone()[0] == 100
    assert finance.execute("SELECT soft_balance FROM points_wallets WHERE user_id=1").fetchone()[0] == 95
    assert finance.execute("SELECT soft_balance FROM points_wallets WHERE user_id=9").fetchone()[0] == 5
    assert finance.execute(
        "SELECT COUNT(*) FROM points_ledger WHERE action_type LIKE 'video_tip_%'"
    ).fetchone()[0] == 3
    finance.close()
    core.close()
