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
    assert "export_server_encrypted_plaintext.sh" in text
    assert "restore_database_catalog_from_bundle.sh" in text
    assert "recovery_action.lock" in text
    assert "stages the bundled `database/` first" in text
    assert "copies/stages the bundle DB instead of moving or deleting the bundle copy" in text
    assert "owner user no longer exists are reassigned to root" in text
    assert "--privacy-mode e2ee" in text
    assert "--prompt-e2ee-passphrase" in text
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
    assert "export_server_encrypted_plaintext.sh" in body
    assert "restore_database_catalog_from_bundle.sh" in body
    assert "recovery_action.lock" in body
    assert "recovery action already selected" in body
    assert "recovery action locked to plaintext export" in body
    assert "recovery action locked to catalog restore" in body
    assert "status=started" in body
    assert "status=completed" in body
    assert "database.before-orphan-catalog-restore" in body
    assert "STAGED_DATABASE" in body
    assert "repair_missing_file_catalog_owners" in body
    assert "username=char(114,111,111,116)" in body
    assert "owner no longer exists to root user id" in body
    assert "for table in uploaded_files storage_files storage_folders storage_share_links cloud_file_refs encrypted_file_keys album_files albums album_share_links" in body
    assert 'root_id=\\$(sqlite3 "\\$db"' in body
    assert '"\\$(sqlite3 "\\$db"' in body
    assert 'UPDATE \\$table SET owner_user_id=\\$root_id' in body
    assert r'cp -a "\$BUNDLE_DATABASE/." "\$STAGED_DATABASE/"' in body
    assert r'mv "\$STAGED_DATABASE" "\$TARGET_DATABASE"' in body
    assert "refusing: a server process appears to be running" in body
    assert r'--db "\$BUNDLE_DATABASE/database.db"' in body
    assert r'--storage-root "\$BUNDLE_STORAGE"' in body
    assert r'--key-file "\$BUNDLE_KEY"' in body
    assert "--confirm-plaintext-output" in body
    assert "Strict E2EE files cannot be decrypted with .filekey" in body
    assert "E2EE ciphertext and metadata are also preserved" in body
    assert "--privacy-mode e2ee" in body
    assert "--prompt-e2ee-passphrase" in body
    assert "hackme_e2ee_plaintext_export" in body
    assert "Original file owners are preserved" in body
    assert "Option 1: decrypt server_encrypted files to a plaintext folder" in body
    assert "Option 2: import the pre-reset database/catalog metadata and orphaned encrypted storage files back after reset" in body
    assert "post_reset_storage_backup" in body
    assert "restored staged pre-reset database/catalog metadata" in body
    assert "restored orphaned storage contents" in body
    assert "post-reset storage root starts clean" in body
    assert "prompt_reset_recovery_action" in text
    assert "choose exactly one helper later" in text
    assert "Choose reset recovery action [1/2/skip]" in text

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


def test_restart_shortcut_pins_effective_runtime_root():
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("write_restart_shortcut_script() {")
    end = text.index("load_local_capacity_defaults() {", start)
    body = text[start:end]

    assert 'append_arg_if_value restart_args --runtime-root "$RUNTIME_ROOT"' in body
    assert 'append_arg_if_value restart_args --runtime-root "$CUSTOM_RUNTIME_ROOT"' not in body


def test_develop_launcher_refuses_source_runtime_layouts():
    body = SCRIPT.read_text(encoding="utf-8")

    assert "source-checkout runtime is disabled" in body
    assert "runtime root must stay outside the source checkout" in body
    assert "run root must resolve below /tmp" in body
    assert "Retired unsafe aliases" in body
