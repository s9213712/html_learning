from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "test_for_develop.sh"


def _reset_function_body() -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    start = text.index("reset_runtime_state() {")
    end = text.index("runtime_storage_inside_runtime()", start)
    return text[start:end]


def test_reset_help_documents_preserved_server_secret_material():
    text = SCRIPT.read_text(encoding="utf-8")

    assert "preserves storage/, venv/, and server-side secret/key" in text
    assert "preserved storage/ files become orphaned" in text
    assert "scripts/admin/decrypt_server_files.py" in text
    assert "pre-reset database/catalog metadata and .filekey" in text
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
    assert "preserved storage files may become orphaned" in body
    assert "server_encrypted exports need pre-reset DB metadata plus .filekey" in body
    assert "scripts/admin/decrypt_server_files.py" in body
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
