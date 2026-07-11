from pathlib import Path

from services.platform.settings import DEFAULT_SETTINGS, MANAGEMENT_ONLY_RESET_SETTINGS


ROOT = Path(__file__).resolve().parents[3]
ROLLOUT_ONLY_FEATURE_FLAGS = {
    "feature_comfyui_legacy_import_enabled",
}


def test_initial_deploy_security_defaults_enable_integrity_and_audit_chain():
    assert DEFAULT_SETTINGS["audit_chain_enabled"] is True
    assert DEFAULT_SETTINGS["integrity_guard_enabled"] is True


def test_initial_deploy_defaults_only_enable_management_and_security_modules():
    enabled_features = {
        key for key, value in DEFAULT_SETTINGS.items()
        if key.startswith("feature_") and value is True and key not in ROLLOUT_ONLY_FEATURE_FLAGS
    }
    assert enabled_features == {
        "feature_accounts_enabled",
        "feature_audit_log_enabled",
        "feature_violation_center_enabled",
        "feature_system_health_enabled",
        "feature_identity_governance_enabled",
        "feature_member_governance_enabled",
        "feature_server_modes_enabled",
        "feature_snapshot_restore_enabled",
        "feature_health_center_enabled",
        "feature_reports_notifications_enabled",
        "feature_personalization_enabled",
        "feature_points_chain_enabled",
    }
    assert DEFAULT_SETTINGS["feature_forum_core_enabled"] is False
    assert DEFAULT_SETTINGS["feature_comfyui_enabled"] is False
    assert DEFAULT_SETTINGS["feature_ai_agent_enabled"] is False
    assert DEFAULT_SETTINGS["feature_games_enabled"] is False
    assert DEFAULT_SETTINGS["feature_privacy_uploads_enabled"] is False
    assert DEFAULT_SETTINGS["feature_storage_albums_enabled"] is False
    assert DEFAULT_SETTINGS["feature_trading_enabled"] is False


def test_runtime_reset_keeps_integrity_and_audit_chain_enabled():
    assert MANAGEMENT_ONLY_RESET_SETTINGS["audit_chain_enabled"] is True
    assert MANAGEMENT_ONLY_RESET_SETTINGS["integrity_guard_enabled"] is True


def test_security_defaults_include_login_autofill_and_notification_mute_settings():
    assert DEFAULT_SETTINGS["login_autofill_block_enabled"] is False
    assert DEFAULT_SETTINGS["notification_muted_types"] == ""


def test_server_upload_request_limit_is_env_driven_and_has_413_handler():
    server_py = (ROOT / "server.py").read_text(encoding="utf-8")
    assert 'HTML_LEARNING_MAX_CONTENT_MB' in server_py
    assert "def _configured_max_content_mb():" in server_py
    assert '_env_int("HTML_LEARNING_MAX_CONTENT_MB", 8192, minimum=128)' in server_py
    assert '_load_db_setting_value(DB_PATH, "server_max_content_mb")' in server_py
    assert "MAX_UPLOAD_REQUEST_MB = _configured_max_content_mb()" in server_py
    assert 'app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_REQUEST_MB * 1024 * 1024' in server_py
    assert "@app.errorhandler(RequestEntityTooLarge)" in server_py
    assert '"error": "request_too_large"' in server_py
    assert '"max_request_mb": limit_mb' in server_py


def test_runtime_artifacts_default_to_repo_runtime_root_and_not_repo_root():
    server_py = (ROOT / "server.py").read_text(encoding="utf-8")
    assert 'default_runtime_root,' in server_py
    assert 'RUNTIME_DIR = _env_path("HACKME_RUNTIME_DIR", default_runtime_root())' in server_py
    assert 'DB_DIR = _env_path("HTML_LEARNING_DB_DIR", os.path.join(RUNTIME_DIR, "database"))' in server_py
    assert 'CHESS_ENGINE_DB_PATH = _env_path("HTML_LEARNING_CHESS_ENGINE_DB_PATH", os.path.join(RUNTIME_DIR, "games", "models", "chess_experiment.db"))' in server_py
    assert 'LOG_DIR = _env_path("HTML_LEARNING_LOG_DIR", os.path.join(RUNTIME_DIR, "logs"))' in server_py
    assert 'STORAGE_DIR = _env_path("HTML_LEARNING_STORAGE_DIR", os.path.join(RUNTIME_DIR, "storage"))' in server_py
    assert '"file_roots": [' in server_py
    assert 'CHAT_DIR,' in server_py
    assert 'STORAGE_DIR,' in server_py
    assert 'os.path.join(BASE_DIR, "uploads")' not in server_py
    assert 'os.path.join(BASE_DIR, "avatars")' not in server_py
    assert 'os.path.join(BASE_DIR, "attachments")' not in server_py
    assert 'os.path.join(BASE_DIR, "media")' not in server_py
    assert 'CHAIN_SEED_PATH = _runtime_path("HTML_LEARNING_CHAIN_SEED_PATH", ".chain_seed")' in server_py
    assert 'CERT_FILE = _runtime_path("HTML_LEARNING_CERT_FILE", "cert.pem")' in server_py
    assert 'KEY_FILE = _runtime_path("HTML_LEARNING_KEY_FILE", "key.pem")' in server_py


def test_server_api_unhandled_errors_return_json():
    server_py = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "@app.errorhandler(Exception)" in server_py
    assert '"error": "internal_server_error"' in server_py
    assert 'if request.path.startswith("/api")' in server_py
