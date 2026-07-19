import fcntl
import hashlib
import hmac
import json
import os
import sqlite3
import stat
import tarfile
import threading
import time
from datetime import datetime

import jsonschema

from scripts.testing.audit_evidence_triad import (
    SCHEMA_PATH,
    SCHEMA_VERSION,
    ARCHIVE_SCHEMA_FILENAME,
    ArchiveLimits,
    AuditEvidencePaths,
    CaptureLimits,
    canonical_json,
    capture_audit_evidence,
    create_audit_evidence_archive,
    validate_audit_evidence_archive,
    validate_audit_evidence_receipt,
)
from services.server.database import get_audit_db
from services.system import audit as audit_service


def _hashes(key, previous_hash, entry):
    entry_hash = hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()
    chain_hash = hmac.new(
        key,
        f"{previous_hash}:{entry_hash}".encode("utf-8"),
        "sha256",
    ).hexdigest()
    return entry_hash, chain_hash


def _build_runtime(tmp_path, *, count=3, latest_index=0):
    runtime = tmp_path / "runtime"
    database_dir = runtime / "database"
    logs_dir = runtime / "logs"
    anchors_dir = runtime / "anchors"
    for directory in (runtime, database_dir, logs_dir, anchors_dir):
        directory.mkdir(parents=True, exist_ok=True)
    seed = "72a48e117a8a74081b5784ad0c4fa619e2d77f96166c20e3"
    key = bytes.fromhex("1f" * 32)
    (runtime / ".chain_seed").write_text(seed, encoding="utf-8")
    (runtime / ".integrity_key").write_bytes(key)
    os.chmod(runtime / ".chain_seed", 0o600)
    os.chmod(runtime / ".integrity_key", 0o600)

    database = database_dir / "audit.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE secure_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ip TEXT,
            user TEXT,
            success INTEGER NOT NULL DEFAULT 0,
            ua TEXT,
            detail TEXT,
            prev_hash TEXT NOT NULL DEFAULT '',
            entry_hash TEXT NOT NULL DEFAULT '',
            chain_hash TEXT NOT NULL DEFAULT ''
        )
        """
    )
    rows = []
    previous_hash = seed
    for index in range(count):
        entry = {
            "ts": f"2026-07-15T12:00:{index:02d}.000",
            "action": f"unit_event_{index + 1}",
            "ip": "127.0.0.1",
            "user": "unit-user",
            "success": index % 2 == 0,
            "ua": "pytest",
            "detail": f"detail-{index + 1}",
        }
        entry_hash, chain_hash = _hashes(key, previous_hash, entry)
        cursor = connection.execute(
            """
            INSERT INTO secure_audit
                (ts, action, ip, user, success, ua, detail, prev_hash, entry_hash, chain_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["ts"],
                entry["action"],
                entry["ip"],
                entry["user"],
                1 if entry["success"] else 0,
                entry["ua"],
                entry["detail"],
                previous_hash,
                entry_hash,
                chain_hash,
            ),
        )
        audit_id = int(cursor.lastrowid)
        log_entry = {
            **entry,
            "_audit_id": audit_id,
            "_prev_hash": previous_hash,
            "_entry_hash": entry_hash,
            "_chain_hash": chain_hash,
        }
        rows.append(
            {
                "audit_id": audit_id,
                "entry": entry,
                "log": log_entry,
                "prev_hash": previous_hash,
                "entry_hash": entry_hash,
                "chain_hash": chain_hash,
            }
        )
        previous_hash = chain_hash
    connection.commit()
    connection.close()

    audit_log = logs_dir / "audit.log"
    audit_log.write_text(
        "".join(canonical_json(row["log"]) + "\n" for row in rows),
        encoding="utf-8",
    )
    if rows:
        anchor_row = rows[latest_index]
        anchor = {
            "ts": "2026-07-15T12:30:00.000",
            "audit_id": anchor_row["audit_id"],
            "entry_hash": anchor_row["entry_hash"],
            "chain_hash": anchor_row["chain_hash"],
            "reason": "interval",
        }
        encoded = canonical_json(anchor) + "\n"
        (anchors_dir / "audit_head.jsonl").write_text(encoded, encoding="utf-8")
        (anchors_dir / "audit_head_latest.json").write_text(encoded, encoding="utf-8")
    return runtime, seed, key, rows


def _capture(tmp_path, runtime, *, mode="online", name="capture", limits=None):
    receipt = capture_audit_evidence(
        paths=AuditEvidencePaths.for_runtime(runtime),
        output_dir=tmp_path / name,
        target="primary",
        mode=mode,
        limits=limits,
    )
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(receipt)
    return receipt


def _error_codes(receipt):
    return {error["code"] for error in receipt["errors"]}


def _tar_info(name, content, *, member_type=tarfile.REGTYPE, linkname=""):
    info = tarfile.TarInfo(name)
    info.size = len(content) if member_type == tarfile.REGTYPE else 0
    info.mode = 0o400
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = member_type
    info.linkname = linkname
    return info


def _write_tar(path, members):
    import io

    with tarfile.open(path, "w", format=tarfile.USTAR_FORMAT) as bundle:
        for name, content, member_type, linkname in members:
            info = _tar_info(
                name, content, member_type=member_type, linkname=linkname
            )
            bundle.addfile(
                info,
                io.BytesIO(content) if member_type == tarfile.REGTYPE else None,
            )
    os.chmod(path, 0o600)


def _read_tar_member_bytes(path):
    values = []
    with tarfile.open(path, "r:") as bundle:
        for member in bundle:
            extracted = bundle.extractfile(member)
            values.append(
                (
                    member.name,
                    extracted.read() if extracted is not None else b"",
                    member.type,
                    member.linkname,
                )
            )
    return values


def _archive_pins(path):
    content = path.read_bytes()
    return {
        "expected_sha256": hashlib.sha256(content).hexdigest(),
        "expected_size": len(content),
    }


def test_online_capture_accepts_interval_anchor_prefix_and_matches_schema(tmp_path):
    runtime, seed, key, _rows = _build_runtime(tmp_path, count=3, latest_index=0)

    receipt = _capture(tmp_path, runtime, mode="online")

    assert receipt["schema_version"] == SCHEMA_VERSION
    assert receipt["ok"] is True
    assert receipt["verdict"] == "PASS"
    assert receipt["counts"] == {
        "db_rows": 3,
        "log_entries": 3,
        "anchor_history_entries": 1,
        "rows_after_latest": 2,
    }
    assert all(receipt["invariants"].values())
    persisted = (tmp_path / "capture" / "receipt.json").read_text(encoding="utf-8")
    assert seed not in persisted
    assert key.hex() not in persisted
    assert not (tmp_path / "capture" / ".chain_seed").exists()
    assert not (tmp_path / "capture" / ".integrity_key").exists()
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(receipt)


def test_current_audit_service_output_is_accepted(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    for directory in (
        runtime,
        runtime / "database",
        runtime / "logs",
        runtime / "anchors",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    seed = "f2a48e117a8a74081b5784ad0c4fa619e2d77f96166c20e4"
    key = bytes.fromhex("2a" * 32)
    (runtime / ".chain_seed").write_text(seed, encoding="utf-8")
    (runtime / ".integrity_key").write_bytes(key)
    database = runtime / "database" / "audit.db"
    audit_service.configure_audit_service(
        get_db=lambda: get_audit_db(str(database)),
        chain_seed=seed,
        integrity_key=key,
        audit_log_path=str(runtime / "logs" / "audit.log"),
        audit_anchor_path=str(runtime / "anchors" / "audit_head.jsonl"),
        audit_anchor_latest_path=str(runtime / "anchors" / "audit_head_latest.json"),
        audit_anchor_interval_seconds=60,
    )
    monkeypatch.setattr(audit_service, "_last_audit_anchor_at", 0.0)
    audit_service.audit(
        "real_writer_one",
        "127.0.0.1",
        user="service-user",
        success=True,
        ua="pytest",
        detail="one",
    )
    audit_service.audit(
        "real_writer_two",
        "127.0.0.1",
        user="service-user",
        success=False,
        ua="pytest",
        detail="two",
    )

    receipt = _capture(tmp_path, runtime, name="service-capture")

    assert receipt["ok"] is True
    assert receipt["counts"]["db_rows"] == 2
    assert receipt["counts"]["rows_after_latest"] == 1


def test_sealed_capture_forces_exact_db_head_anchor(tmp_path):
    runtime, _seed, _key, rows = _build_runtime(tmp_path, count=3, latest_index=0)

    receipt = _capture(tmp_path, runtime, mode="sealed")

    assert receipt["ok"] is True
    assert receipt["capture"]["head_anchor"] == {
        "attempted": True,
        "performed": True,
        "reason": "formal_evidence_seal",
        "audit_id": rows[-1]["audit_id"],
        "entry_hash": rows[-1]["entry_hash"],
        "chain_hash": rows[-1]["chain_hash"],
    }
    assert receipt["counts"]["anchor_history_entries"] == 2
    assert receipt["counts"]["rows_after_latest"] == 0
    assert receipt["heads"]["anchor_latest"]["audit_id"] == rows[-1]["audit_id"]
    assert receipt["heads"]["anchor_latest"]["reason"] == "formal_evidence_seal"
    latest = json.loads(
        (runtime / "anchors" / "audit_head_latest.json").read_text(encoding="utf-8")
    )
    assert latest["audit_id"] == rows[-1]["audit_id"]


def test_cross_source_tampering_is_fail_product(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=3, latest_index=2)
    connection = sqlite3.connect(runtime / "database" / "audit.db")
    connection.execute("UPDATE secure_audit SET detail='tampered' WHERE id=2")
    connection.commit()
    connection.close()

    db_receipt = _capture(tmp_path, runtime, name="db-capture")

    assert db_receipt["verdict"] == "FAIL_PRODUCT"
    assert "db_entry_hash_mismatch" in _error_codes(db_receipt)
    assert db_receipt["invariants"]["db_chain_valid"] is False

    runtime2, _seed2, _key2, _rows2 = _build_runtime(
        tmp_path / "second", count=3, latest_index=2
    )
    log_path = runtime2 / "logs" / "audit.log"
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    entries[1]["detail"] = "tampered"
    log_path.write_text(
        "".join(canonical_json(entry) + "\n" for entry in entries), encoding="utf-8"
    )

    log_receipt = _capture(tmp_path, runtime2, name="second-log-capture")

    assert log_receipt["verdict"] == "FAIL_PRODUCT"
    assert {"log_chain_mismatch", "log_db_mismatch"}.issubset(_error_codes(log_receipt))
    assert log_receipt["invariants"]["audit_log_db_bijection"] is False


def test_anchor_reference_tampering_is_fail_product(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=2, latest_index=1)
    history_path = runtime / "anchors" / "audit_head.jsonl"
    latest_path = runtime / "anchors" / "audit_head_latest.json"
    anchor = json.loads(latest_path.read_text(encoding="utf-8"))
    anchor["chain_hash"] = "0" * 64
    encoded = canonical_json(anchor) + "\n"
    history_path.write_text(encoded, encoding="utf-8")
    latest_path.write_text(encoded, encoding="utf-8")

    receipt = _capture(tmp_path, runtime)

    assert receipt["verdict"] == "FAIL_PRODUCT"
    assert {"anchor_db_mismatch", "latest_db_mismatch"}.issubset(_error_codes(receipt))
    assert receipt["invariants"]["anchor_history_references_db"] is False


def test_noncanonical_db_value_is_product_failure_not_validator_crash(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=1, latest_index=0)
    connection = sqlite3.connect(runtime / "database" / "audit.db")
    connection.execute(
        "UPDATE secure_audit SET detail=? WHERE id=1",
        (sqlite3.Binary(b"\xff\x00"),),
    )
    connection.commit()
    connection.close()

    receipt = _capture(tmp_path, runtime, name="blob-capture")

    assert receipt["verdict"] == "FAIL_PRODUCT"
    assert "db_entry_shape" in _error_codes(receipt)
    assert "validator_internal_error" not in _error_codes(receipt)


def test_malformed_latest_still_emits_schema_valid_failure_receipt(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=1, latest_index=0)
    (runtime / "anchors" / "audit_head_latest.json").write_text(
        '{"unexpected":true}\n', encoding="utf-8"
    )

    receipt = _capture(tmp_path, runtime, name="malformed-latest")

    assert receipt["verdict"] == "FAIL_PRODUCT"
    assert "latest_shape" in _error_codes(receipt)
    assert receipt["heads"]["anchor_latest"] is None


def test_sqlite_backup_includes_committed_wal_tail(tmp_path):
    runtime, seed, key, rows = _build_runtime(tmp_path, count=2, latest_index=1)
    database = runtime / "database" / "audit.db"
    connection = sqlite3.connect(database)
    assert str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"
    connection.execute("PRAGMA wal_autocheckpoint=0")
    previous_hash = rows[-1]["chain_hash"]
    entry = {
        "ts": "2026-07-15T12:00:59.000",
        "action": "wal_tail",
        "ip": "127.0.0.1",
        "user": "unit-user",
        "success": True,
        "ua": "pytest",
        "detail": "committed-in-wal",
    }
    entry_hash, chain_hash = _hashes(key, previous_hash, entry)
    cursor = connection.execute(
        """
        INSERT INTO secure_audit
            (ts, action, ip, user, success, ua, detail, prev_hash, entry_hash, chain_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry["ts"], entry["action"], entry["ip"], entry["user"], 1,
            entry["ua"], entry["detail"], previous_hash, entry_hash, chain_hash,
        ),
    )
    audit_id = int(cursor.lastrowid)
    connection.commit()
    log_entry = {
        **entry,
        "_audit_id": audit_id,
        "_prev_hash": previous_hash,
        "_entry_hash": entry_hash,
        "_chain_hash": chain_hash,
    }
    with (runtime / "logs" / "audit.log").open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(log_entry) + "\n")
    anchor = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "audit_id": audit_id,
        "entry_hash": entry_hash,
        "chain_hash": chain_hash,
        "reason": "interval",
    }
    encoded = canonical_json(anchor) + "\n"
    with (runtime / "anchors" / "audit_head.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(encoded)
    (runtime / "anchors" / "audit_head_latest.json").write_text(encoded, encoding="utf-8")
    assert (runtime / "database" / "audit.db-wal").stat().st_size > 0

    try:
        receipt = _capture(tmp_path, runtime, name="wal-capture")
    finally:
        connection.close()

    assert receipt["ok"] is True
    assert receipt["counts"]["db_rows"] == 3
    snapshot = sqlite3.connect(tmp_path / "wal-capture" / "audit_snapshot.sqlite3")
    try:
        assert snapshot.execute("SELECT action FROM secure_audit ORDER BY id DESC LIMIT 1").fetchone()[0] == "wal_tail"
    finally:
        snapshot.close()
    assert seed not in (tmp_path / "wal-capture" / "receipt.json").read_text(encoding="utf-8")


def test_capture_waits_for_dedicated_mutation_lock(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=1, latest_index=0)
    lock_path = runtime / "logs" / "audit.log.mutation.lock"
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)

    def release():
        time.sleep(0.15)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    releaser = threading.Thread(target=release)
    releaser.start()
    receipt = _capture(
        tmp_path,
        runtime,
        name="lock-capture",
        limits=CaptureLimits(lock_timeout_seconds=1.0),
    )
    releaser.join(timeout=2)

    assert receipt["ok"] is True
    assert receipt["capture"]["mutation_lock_wait_ms"] >= 80
    assert receipt["invariants"]["mutation_lock_acquired"] is True


def test_symlink_source_fails_closed_without_secret_leak(tmp_path):
    runtime, seed, key, _rows = _build_runtime(tmp_path, count=1, latest_index=0)
    outside = tmp_path / "outside.log"
    outside.write_text("{}\n", encoding="utf-8")
    audit_log = runtime / "logs" / "audit.log"
    audit_log.unlink()
    audit_log.symlink_to(outside)

    receipt = _capture(tmp_path, runtime, name="unsafe-capture")

    assert receipt["ok"] is False
    assert receipt["verdict"] == "FAIL_HARNESS"
    assert "unsafe_path" in _error_codes(receipt)
    persisted = (tmp_path / "unsafe-capture" / "receipt.json").read_text(encoding="utf-8")
    assert seed not in persisted
    assert key.hex() not in persisted


def test_empty_chain_requires_no_stale_latest(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=0)

    clean = _capture(tmp_path, runtime, mode="sealed", name="empty-clean")

    assert clean["ok"] is True
    assert clean["counts"]["db_rows"] == 0
    assert clean["capture"]["head_anchor"]["reason"] == "empty_chain"

    stale = {
        "ts": "2026-07-15T12:00:00.000",
        "audit_id": 1,
        "entry_hash": "1" * 64,
        "chain_hash": "2" * 64,
        "reason": "interval",
    }
    (runtime / "anchors" / "audit_head_latest.json").write_text(
        canonical_json(stale) + "\n", encoding="utf-8"
    )
    failed = _capture(tmp_path, runtime, mode="online", name="empty-stale")

    assert failed["verdict"] == "FAIL_PRODUCT"
    assert "latest_for_empty_db" in _error_codes(failed)


def test_archive_is_deterministic_private_secret_free_and_validates_by_path_or_fd(
    tmp_path,
):
    runtime, _seed, _key, _rows = _build_runtime(
        tmp_path, count=3, latest_index=0
    )
    receipt = _capture(tmp_path, runtime, name="archive-source")
    first = tmp_path / "triad-one.tar"
    second = tmp_path / "triad-two.tar"

    first_record = create_audit_evidence_archive(
        output_dir=tmp_path / "archive-source", archive_path=first
    )
    second_record = create_audit_evidence_archive(
        output_dir=tmp_path / "archive-source", archive_path=second
    )

    assert first.read_bytes() == second.read_bytes()
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert first.stat().st_nlink == 1
    assert first_record["sha256"] == second_record["sha256"]
    assert first_record["secret_files_included"] is False
    with tarfile.open(first, "r:") as bundle:
        names = bundle.getnames()
        assert names == [
            "receipt.json",
            ARCHIVE_SCHEMA_FILENAME,
            "audit_snapshot.sqlite3",
            "audit.log",
            "audit_head.jsonl",
            "audit_head_latest.json",
        ]
        assert ".chain_seed" not in names
        assert ".integrity_key" not in names

    path_result = validate_audit_evidence_archive(
        first,
        required_mode="online",
        required_target="primary",
        expected_sha256=first_record["sha256"],
        expected_size=first_record["size"],
    )
    assert path_result["ok"] is True
    assert path_result["classification"] == "PASS"
    assert path_result["receipt"]["payload"] == receipt
    assert path_result["receipt"]["sha256"] == path_result["members"]["receipt.json"]["sha256"]
    assert all(path_result["rederived"]["invariants"].values())

    descriptor = os.open(first, os.O_RDONLY)
    try:
        descriptor_result = validate_audit_evidence_archive(
            descriptor=descriptor,
            required_mode="online",
            required_target="primary",
            expected_sha256=first_record["sha256"],
            expected_size=first_record["size"],
        )
    finally:
        os.close(descriptor)
    assert descriptor_result["ok"] is True
    assert descriptor_result["archive"]["descriptor_pinned"] is True


def test_empty_chain_archive_contains_only_present_artifacts_and_validates(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=0)
    _capture(tmp_path, runtime, mode="sealed", name="empty-archive-source")
    archive = tmp_path / "empty-triad.tar"

    record = create_audit_evidence_archive(
        output_dir=tmp_path / "empty-archive-source", archive_path=archive
    )
    result = validate_audit_evidence_archive(
        archive,
        required_mode="sealed",
        required_target="primary",
        expected_sha256=record["sha256"],
        expected_size=record["size"],
    )

    assert result["ok"] is True
    assert set(result["members"]) == {
        "receipt.json",
        ARCHIVE_SCHEMA_FILENAME,
        "audit_snapshot.sqlite3",
        "audit.log",
    }
    assert result["rederived"]["counts"]["db_rows"] == 0


def test_archive_rejects_traversal_symlink_duplicate_secret_and_bomb_members(tmp_path):
    cases = {
        "traversal": [
            ("../receipt.json", b"{}", tarfile.REGTYPE, ""),
        ],
        "symlink": [
            ("receipt.json", b"", tarfile.SYMTYPE, "/etc/passwd"),
        ],
        "duplicate": [
            ("receipt.json", b"{}", tarfile.REGTYPE, ""),
            ("receipt.json", b"{}", tarfile.REGTYPE, ""),
        ],
        "secret": [
            (".integrity_key", b"do-not-copy", tarfile.REGTYPE, ""),
        ],
    }
    expected_codes = {
        "traversal": "archive_unexpected_member",
        "symlink": "archive_unsafe_member_type",
        "duplicate": "archive_duplicate_member",
        "secret": "archive_unexpected_member",
    }
    for name, members in cases.items():
        archive = tmp_path / f"unsafe-{name}.tar"
        _write_tar(archive, members)
        result = validate_audit_evidence_archive(
            archive,
            required_mode="online",
            required_target="primary",
            **_archive_pins(archive),
        )
        assert result["ok"] is False
        assert result["classification"] == "FAIL_HARNESS"
        assert expected_codes[name] in _error_codes(result)

    runtime, _seed, _key, _rows = _build_runtime(
        tmp_path / "bomb", count=1, latest_index=0
    )
    _capture(tmp_path / "bomb", runtime, name="source")
    archive = tmp_path / "bounded.tar"
    create_audit_evidence_archive(
        output_dir=tmp_path / "bomb" / "source", archive_path=archive
    )
    result = validate_audit_evidence_archive(
        archive,
        required_mode="online",
        required_target="primary",
        limits=ArchiveLimits(receipt_bytes=16),
        **_archive_pins(archive),
    )
    assert result["ok"] is False
    assert "archive_member_oversize" in _error_codes(result)


def test_archive_validator_rederives_bytes_instead_of_trusting_green_receipt(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=2, latest_index=1)
    receipt = _capture(tmp_path, runtime, name="coherently-green")
    output = tmp_path / "coherently-green"
    log_path = output / "audit.log"
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    entries[0]["detail"] = "archive-bytes-do-not-match-db"
    log_path.write_text(
        "".join(canonical_json(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )
    changed = log_path.read_bytes()
    receipt["artifacts"]["audit_log"]["size"] = len(changed)
    receipt["artifacts"]["audit_log"]["sha256"] = hashlib.sha256(changed).hexdigest()
    (output / "receipt.json").write_text(
        canonical_json(receipt) + "\n", encoding="utf-8"
    )
    archive = tmp_path / "coherently-green.tar"

    creation = create_audit_evidence_archive(output_dir=output, archive_path=archive)
    result = validate_audit_evidence_archive(
        archive,
        required_mode="online",
        required_target="primary",
        expected_sha256=creation["sha256"],
        expected_size=creation["size"],
    )

    assert creation["ok"] is False
    assert creation["classification"] == "FAIL_PRODUCT"
    assert result["ok"] is False
    assert result["classification"] == "FAIL_PRODUCT"
    assert {
        "log_entry_hash_mismatch",
        "log_db_mismatch",
    }.issubset(_error_codes(result))
    assert result["receipt"]["payload"]["ok"] is True
    assert result["receipt_validation"]["ok"] is True


def test_archive_validator_rejects_receipt_digest_lie_and_trailing_data(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=1, latest_index=0)
    _capture(tmp_path, runtime, name="digest-source")
    valid = tmp_path / "valid.tar"
    create_audit_evidence_archive(
        output_dir=tmp_path / "digest-source", archive_path=valid
    )
    members = _read_tar_member_bytes(valid)
    rewritten = []
    for name, content, member_type, linkname in members:
        if name == "audit.log":
            content += b"\n"
        rewritten.append((name, content, member_type, linkname))
    digest_lie = tmp_path / "digest-lie.tar"
    _write_tar(digest_lie, rewritten)

    result = validate_audit_evidence_archive(
        digest_lie,
        required_mode="online",
        required_target="primary",
        **_archive_pins(digest_lie),
    )
    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert "receipt_contract:artifact_file_digest_mismatch:audit_log" in _error_codes(result)

    trailing = tmp_path / "trailing.tar"
    trailing.write_bytes(valid.read_bytes() + b"hidden")
    os.chmod(trailing, 0o600)
    trailing_result = validate_audit_evidence_archive(
        trailing,
        required_mode="online",
        required_target="primary",
        **_archive_pins(trailing),
    )
    assert trailing_result["ok"] is False
    assert "archive_trailing_or_truncated_data" in _error_codes(trailing_result)


def test_archive_descriptor_must_match_supplied_path(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=1, latest_index=0)
    _capture(tmp_path, runtime, name="descriptor-source")
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"
    create_audit_evidence_archive(
        output_dir=tmp_path / "descriptor-source", archive_path=first
    )
    create_audit_evidence_archive(
        output_dir=tmp_path / "descriptor-source", archive_path=second
    )
    descriptor = os.open(first, os.O_RDONLY)
    try:
        result = validate_audit_evidence_archive(
            second,
            descriptor=descriptor,
            required_mode="online",
            required_target="primary",
            **_archive_pins(second),
        )
    finally:
        os.close(descriptor)

    assert result["ok"] is False
    assert result["classification"] == "FAIL_HARNESS"
    assert "archive_descriptor_path_mismatch" in _error_codes(result)


def test_archive_path_must_be_private_regular_and_single_link(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=1, latest_index=0)
    _capture(tmp_path, runtime, name="secure-path-source")
    archive = tmp_path / "secure.tar"
    create_audit_evidence_archive(
        output_dir=tmp_path / "secure-path-source", archive_path=archive
    )

    os.chmod(archive, 0o644)
    public_result = validate_audit_evidence_archive(
        archive,
        required_mode="online",
        required_target="primary",
        **_archive_pins(archive),
    )
    assert public_result["ok"] is False
    assert "archive_permissions" in _error_codes(public_result)

    os.chmod(archive, 0o600)
    hardlink = tmp_path / "second-link.tar"
    os.link(archive, hardlink)
    hardlink_result = validate_audit_evidence_archive(
        archive,
        required_mode="online",
        required_target="primary",
        **_archive_pins(archive),
    )
    assert hardlink_result["ok"] is False
    assert "unsafe_path" in _error_codes(hardlink_result)


def test_receipt_artifact_hashing_rejects_fifo_and_sparse_oversize_without_opening(
    tmp_path,
):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=1, latest_index=0)
    receipt = _capture(tmp_path, runtime, name="hash-safety-source")
    output = tmp_path / "hash-safety-source"
    log_path = output / "audit.log"
    log_path.unlink()
    os.mkfifo(log_path, 0o600)

    started = time.monotonic()
    fifo_result = validate_audit_evidence_receipt(
        receipt,
        required_mode="online",
        required_target="primary",
        artifact_root=output,
    )
    assert time.monotonic() - started < 1.0
    assert fifo_result["ok"] is False
    assert "artifact_file_unsafe:audit_log" in fifo_result["errors"]

    log_path.unlink()
    with log_path.open("wb") as handle:
        handle.truncate(2 * 1024 * 1024 * 1024 + 1)
    oversize_result = validate_audit_evidence_receipt(
        receipt,
        required_mode="online",
        required_target="primary",
        artifact_root=output,
    )
    assert oversize_result["ok"] is False
    assert "artifact_file_unsafe:audit_log" in oversize_result["errors"]


def test_mixed_product_and_receipt_capture_errors_classify_as_harness(tmp_path):
    runtime, _seed, _key, _rows = _build_runtime(tmp_path, count=2, latest_index=1)
    receipt = _capture(tmp_path, runtime, name="mixed-classification")
    output = tmp_path / "mixed-classification"
    log_path = output / "audit.log"
    entries = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    entries[0]["detail"] = "product-integrity-failure"
    log_path.write_text(
        "".join(canonical_json(entry) + "\n" for entry in entries), encoding="utf-8"
    )
    changed = log_path.read_bytes()
    receipt["artifacts"]["audit_log"]["size"] = len(changed)
    receipt["artifacts"]["audit_log"]["sha256"] = hashlib.sha256(changed).hexdigest()
    for role in ("database", "audit_log"):
        receipt["heads"][role]["entry_hash"] = "0" * 64
        receipt["heads"][role]["chain_hash"] = "1" * 64
    (output / "receipt.json").write_text(
        canonical_json(receipt) + "\n", encoding="utf-8"
    )
    archive = tmp_path / "mixed-classification.tar"

    creation = create_audit_evidence_archive(output_dir=output, archive_path=archive)

    assert creation["ok"] is False
    assert creation["classification"] == "FAIL_HARNESS"
    codes = _error_codes(creation["validation"])
    assert "log_entry_hash_mismatch" in codes
    assert "receipt_heads_mismatch" in codes
