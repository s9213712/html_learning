"""Service-container wiring extracted from ``server.py``."""

from __future__ import annotations

import os

from services.users.auth import configure_auth_service
from services.platform.bootstrap import configure_bootstrap_service
from services.chat.support import configure_chat_support_service
from services.games.chess_engine import ChessExperimentStore
from services.points_chain import PointsLedgerService
from services.security.events import configure_security_events_service
from services.platform.settings import configure_settings_service
from services.snapshots import SnapshotService, ServerModeService
from services.system.audit import _chain_hash, configure_audit_service, reset_audit_chain_with_event
from services.system.integrity_guard import IntegrityGuard
from services.trading.streams import TradingPriceStreamHub
from services.trading.trading_engine import TradingEngineService
from services.governance.violations import configure_violations_service


def build_runtime_services(*, config, deps):
    configure_settings_service(
        get_db=deps["get_db"],
        load_json=deps["load_json"],
        base_dir=config["base_dir"],
    )
    configure_auth_service(
        get_db=deps["get_db"],
        get_auth_db=deps.get("get_auth_db", deps["get_db"]),
        get_readonly_auth_db=deps.get("get_readonly_auth_db"),
        get_user_by_username=deps["get_user_by_username"],
        fernet=deps["fernet"],
        get_client_ip=deps["get_client_ip"],
        session_ttl=deps["session_ttl"],
        csrf_token_ttl=deps["csrf_token_ttl"],
        session_idle_timeout=deps["session_idle_timeout"],
        tester_token_user_lookup=deps["tester_token_user_lookup"],
        get_runtime_server_mode=deps["get_runtime_server_mode"],
        get_system_settings=deps.get("get_system_settings"),
        csrf_secret=deps.get("csrf_secret"),
    )
    configure_audit_service(
        get_db=deps.get("get_audit_db", deps["get_db"]),
        chain_seed=config["chain_seed"],
        integrity_key=config["integrity_key"],
        audit_log_path=config["audit_log_path"],
        audit_anchor_path=config["audit_anchor_path"],
        audit_anchor_latest_path=config["audit_anchor_latest_path"],
        audit_anchor_interval_seconds=config["audit_anchor_interval_seconds"],
    )
    configure_violations_service(
        get_db=deps["get_db"],
        get_system_settings=deps["get_system_settings"],
        audit=deps["audit"],
        get_client_ip=deps["get_client_ip"],
        chain_seed=config["chain_seed"],
        integrity_key=config["integrity_key"],
    )
    configure_security_events_service(
        get_db=deps["get_db"],
        get_system_settings=deps["get_system_settings"],
        audit=deps["audit"],
        is_ip_blocking_enabled=deps["is_ip_blocking_enabled"],
    )
    configure_bootstrap_service(
        get_db=deps["get_db"],
        db_path=os.path.join(config["db_dir"], "bootstrap"),
        schema_path=os.path.join(config["base_dir"], "bootstrap.schema.sql"),
        legacy_fail_log=config["legacy_fail_log"],
        legacy_blocked_ips=config["legacy_blocked_ips"],
        legacy_rate_limit=config["legacy_rate_limit"],
        legacy_audit_log=config["legacy_audit_log"],
        chain_seed=config["chain_seed"],
        chain_hash=_chain_hash,
        load_json=deps["load_json"],
        normalize_text=deps["normalize_text"],
        hash_password=deps["hash_password"],
        verify_password=deps["verify_password"],
        audit=deps["audit"],
        refresh_system_settings=deps["refresh_system_settings"],
        init_system_settings_table=deps["init_system_settings_table"],
        seed_missing_settings=deps["seed_missing_settings"],
        import_legacy_settings_files=deps["import_legacy_settings_files"],
        default_settings=deps["default_settings"],
    )
    configure_chat_support_service(
        chat_dir=config["chat_dir"],
        official_chat_room_name=config["official_chat_room_name"],
        encrypt_field=deps["encrypt_field"],
    )

    finance_get_db = deps.get("get_finance_db", deps["get_db"])
    points_service = PointsLedgerService(
        get_db=finance_get_db,
        chain_secret=config["chain_seed"],
        audit=deps["audit"],
        backup_dir=config["points_chain_backup_dir"],
        mode_reader=deps["get_runtime_server_mode"],
        security_event_recorder=lambda event_type, **kwargs: deps["record_security_event"](
            event_type, deps["get_client_ip"](), **kwargs
        ),
    )
    snapshot_service = SnapshotService(
        get_db=deps["get_db"],
        db_path=config["db_path"],
        base_dir=config["base_dir"],
        runtime_base_dir=config["runtime_secrets_dir"],
        storage_root=config["storage_root"],
        audit=deps["audit"],
        file_roots=config["file_roots"],
        config_files=config["config_files"],
        runtime_secret_files=config["runtime_secret_files"],
        additional_db_paths=config.get("additional_db_paths"),
        reset_points_chain=lambda **kwargs: points_service.reset_runtime_chain(**kwargs),
        reset_audit_chain=reset_audit_chain_with_event,
    )
    integrity_guard = IntegrityGuard(
        base_dir=config["base_dir"],
        manifest_path=config["integrity_manifest_path"],
        signing_key=config["root_integrity_signing_key"],
        get_db=deps["get_db"],
        audit=deps["audit"],
    )
    trading_price_stream_hub = TradingPriceStreamHub(audit=deps["audit"])
    trading_service = TradingEngineService(
        get_db=finance_get_db,
        points_service=points_service,
        audit=deps["audit"],
        stream_hub=trading_price_stream_hub,
    )
    chess_engine_store = ChessExperimentStore(db_path=config["chess_engine_db_path"])
    snapshot_service.set_post_restore_validators(
        [
            ("points_chain", lambda: points_service.verify_chain_bounded_snapshot()),
            ("trading_state", lambda: trading_service.verify_state()),
        ]
    )
    server_mode_service = ServerModeService(
        snapshot_service=snapshot_service,
        get_db=deps["get_db"],
        get_auth_db=deps.get("get_auth_db", deps["get_db"]),
        get_control_db=deps.get("get_control_db", deps["get_db"]),
        audit=deps["audit"],
        integrity_guard=integrity_guard,
        save_settings=deps["save_settings"],
    )
    return {
        "snapshot_service": snapshot_service,
        "integrity_guard": integrity_guard,
        "points_service": points_service,
        "trading_price_stream_hub": trading_price_stream_hub,
        "trading_service": trading_service,
        "chess_engine_store": chess_engine_store,
        "server_mode_service": server_mode_service,
    }
