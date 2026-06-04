from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "test_for_develop.sh"
CATALOG = ROOT / "docs" / "TEST_FOR_DEVELOP_COMMAND_CATALOG.md"


def _reset_function_body() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("reset_runtime_state() {")
    end = text.index("runtime_storage_inside_runtime()", start)
    return text[start:end]


def test_reset_help_documents_preserved_server_secret_material():
    text = CATALOG.read_text(encoding="utf-8")

    assert "## Reset" in text
    assert "`--reset` preserves server-side key material" in text
    assert "The existing pre-reset storage contents are moved into `orphaned_storage/`" in text
    assert "post-reset storage root starts clean" in text
    assert "storage/.reset_orphan_recovery/reset_<timestamp>/" in text
    assert "pre-reset DB/catalog metadata" in text
    assert "decrypt_server_files.py" in text
    assert "restore_database_catalog_from_bundle.sh" in text
    assert "stages the bundled `database/` first" in text
    assert "copies/stages the bundle DB instead of moving or deleting the bundle copy" in text
    assert "moves current post-reset storage contents into `post_reset_storage_backup_<timestamp>`" in text
    assert "moves `orphaned_storage/` contents back into the storage root" in text
    for name in (
        ".filekey",
        ".fkey",
        ".csrfkey",
        ".integrity_key",
        ".chain_seed",
        "cert.pem",
        "key.pem",
    ):
        assert name in text


def test_reset_runtime_state_does_not_delete_server_secret_material():
    body = _reset_function_body()

    assert "preserving storage/, venv/, and server-side secret/key files" in body
    assert "pre-reset storage files move to orphaned_storage/" in body
    assert "write_reset_orphan_recovery_bundle" in body
    assert ".reset_orphan_recovery" in body
    for name in (
        ".filekey",
        ".fkey",
        ".csrfkey",
        ".integrity_key",
        ".chain_seed",
        ".server_mode_log_hmac_key",
        "cert.pem",
        "key.pem",
    ):
        assert f"$RUNTIME_ROOT/{name}" not in body
        assert f"./{name}" not in body

    assert '"$RUNTIME_ROOT/storage"' not in body
    assert '"$RUNTIME_ROOT/venv"' not in body



def test_reset_writes_orphan_recovery_bundle_with_export_material():
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("write_reset_orphan_recovery_bundle() {")
    end = text.index("reset_runtime_state() {", start)
    body = text[start:end]

    assert "$EFFECTIVE_STORAGE_ROOT/.reset_orphan_recovery" in body
    assert "cp -a \"$RUNTIME_ROOT/database/.\"" in body
    assert "scripts/admin/decrypt_server_files.py" in body
    assert "README_SERVER_ENCRYPTED_RECOVERY.txt" in body
    assert "orphaned_storage" in body
    assert "mv \"$storage_item\" \"$bundle_dir/orphaned_storage/\"" in body
    assert ".reset_orphan_recovery" in body
    assert "restore_database_catalog_from_bundle.sh" in body
    assert "database.before-orphan-catalog-restore" in body
    assert "STAGED_DATABASE" in body
    assert r'cp -a "\$BUNDLE_DATABASE/." "\$STAGED_DATABASE/"' in body
    assert r'mv "\$STAGED_DATABASE" "\$TARGET_DATABASE"' in body
    assert "refusing: a server process appears to be running" in body
    assert "--db \"$bundle_dir/database/database.db\"" in body
    assert "--storage-root \"$bundle_dir/orphaned_storage\"" in body
    assert "--key-file \"$bundle_dir/runtime_secrets/.filekey\"" in body
    assert "--confirm-plaintext-output" in body
    assert "Strict E2EE files cannot be decrypted with .filekey" in body
    assert "To import the pre-reset database/catalog metadata and orphaned storage files back after reset" in body
    assert "orphaned storage files back after reset" in body
    assert "post_reset_storage_backup" in body
    assert "restored staged pre-reset database/catalog metadata" in body
    assert "restored orphaned storage contents" in body
    assert "post-reset storage root starts clean" in body

    for name in (
        ".filekey",
        ".fkey",
        ".csrfkey",
        ".integrity_key",
        ".chain_seed",
        ".server_mode_log_hmac_key",
        "cert.pem",
        "key.pem",
    ):
        assert name in body


def test_runtime_maintenance_conflict_is_checked_before_dry_run():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "ensure_single_runtime_maintenance_action()" in text
    dry_run_index = text.index('if [[ "$DRY_RUN" == "1" ]]; then')
    pre_dry_run = text[:dry_run_index]
    assert "ensure_single_runtime_maintenance_action" in pre_dry_run
    assert "choose exactly one of --backup, --restore, --reset, or --delete" in text
