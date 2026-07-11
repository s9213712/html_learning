import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

from services.security.upload_policy import (
    _validate_yara_rules_path,
    file_extension,
    get_cloud_drive_security_policy,
    is_e2ee_privacy_mode,
    normalize_scan_status,
)
from services.security.upload_schema import (
    ALLOWED_CLAMAV_COMMANDS,
    ALLOWED_YARA_COMMANDS,
    ARCHIVE_EXTENSIONS,
    EXECUTABLE_EXTENSIONS,
    EXTENSION_MIME_PREFIXES,
    HIGH_RISK_MAGIC_MIMES,
    MACRO_OFFICE_EXTENSIONS,
    MIME_SIGNATURES,
    OFFICE_EXTENSIONS,
    REENCODABLE_IMAGE_EXTENSIONS,
    ensure_upload_security_schema,
)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_zip_archive_safety(path, *, max_files=200, max_uncompressed_bytes=50 * 1024 * 1024, recursive=False, max_depth=2):
    result = {"ok": True, "reason": "ok", "file_count": 0, "uncompressed_bytes": 0, "max_depth_seen": 0}
    ext = file_extension(path)

    def walk_zip(blob, depth):
        with zipfile.ZipFile(blob) as archive:
            infos = archive.infolist()
            for info in infos:
                if info.is_dir():
                    continue
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    return False, "path_traversal"
                result["file_count"] += 1
                result["max_depth_seen"] = max(result["max_depth_seen"], depth)
                if result["file_count"] > max_files:
                    return False, "too_many_files"
                result["uncompressed_bytes"] += int(info.file_size or 0)
                if result["uncompressed_bytes"] > max_uncompressed_bytes:
                    return False, "zip_bomb"
                if recursive and depth < max_depth and file_extension(info.filename) == ".zip":
                    nested = archive.read(info)
                    ok, reason = walk_zip(BytesIO(nested), depth + 1)
                    if not ok:
                        return False, reason
        return True, "ok"

    def walk_gzip(gzip_path):
        total = 0
        with gzip.open(gzip_path, "rb") as archive:
            while True:
                chunk = archive.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_uncompressed_bytes:
                    return False, "gzip_bomb", total
        return True, "ok", total

    try:
        with open(path, "rb") as handle:
            is_gzip_stream = handle.read(2) == b"\x1f\x8b"
        if (ext == ".gz" or is_gzip_stream) and not str(path).lower().endswith((".tar.gz", ".tgz")):
            ok, reason, total = walk_gzip(path)
            result["file_count"] = 1
            result["uncompressed_bytes"] = total
            return {**result, "ok": ok, "reason": reason}
        ok, reason = walk_zip(path, 0)
        return {**result, "ok": ok, "reason": reason}
    except gzip.BadGzipFile:
        return {**result, "ok": False, "reason": "bad_gzip"}
    except zipfile.BadZipFile:
        return {**result, "ok": False, "reason": "bad_zip"}


def check_office_macro_safety(path, filename=None):
    ext = file_extension(filename or path)
    result = {"ok": True, "reason": "ok", "extension": ext, "macro_indicators": []}
    if ext not in OFFICE_EXTENSIONS:
        return {**result, "reason": "not_office"}
    if ext in MACRO_OFFICE_EXTENSIONS:
        result["macro_indicators"].append("macro_enabled_extension")
    try:
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                names = [name.lower() for name in archive.namelist()]
            for marker in ("vbaproject.bin", "macrosheets/", "xl4macrosheets/"):
                if any(marker in name for name in names):
                    result["macro_indicators"].append(marker.rstrip("/"))
        else:
            with open(path, "rb") as f:
                sample = f.read(1024 * 1024).lower()
            for marker in (b"vba", b"macros", b"attribut vb_"):
                if marker in sample:
                    result["macro_indicators"].append(marker.decode("ascii", errors="ignore"))
    except Exception as exc:
        return {**result, "ok": False, "reason": "office_macro_scan_failed", "error": exc.__class__.__name__}
    if result["macro_indicators"]:
        return {**result, "ok": False, "reason": "macro_detected"}
    return result


def detect_magic_mime(path):
    with open(path, "rb") as f:
        header = f.read(16)
    for signature, mime in MIME_SIGNATURES:
        if header.startswith(signature):
            return mime
    if not header:
        return "application/x-empty"
    if b"\x00" not in header:
        return "text/plain"
    return "application/octet-stream"


def check_magic_mime_safety(path, filename=None, declared_mime=None):
    actual = detect_magic_mime(path)
    ext = file_extension(filename or path)
    expected = EXTENSION_MIME_PREFIXES.get(ext)
    result = {
        "ok": True,
        "reason": "ok",
        "extension": ext,
        "declared_mime": declared_mime or "",
        "detected_mime": actual,
    }
    if actual in HIGH_RISK_MAGIC_MIMES and ext not in EXECUTABLE_EXTENSIONS:
        return {**result, "ok": False, "reason": "executable_magic_mismatch"}
    if expected and actual not in expected and actual != "application/x-empty":
        return {**result, "ok": False, "reason": "extension_mime_mismatch"}
    return result


def reencode_image_strip_metadata(path, *, filename=None, max_pixels=25_000_000):
    ext = file_extension(filename or path)
    if ext not in REENCODABLE_IMAGE_EXTENSIONS:
        return {"ok": True, "result": "not_required", "reason": "not_reencodable_image"}
    try:
        from PIL import Image, ImageOps
    except Exception:
        return {"ok": True, "result": "skipped", "reason": "pillow_not_installed"}

    target = Path(path)
    before_size = target.stat().st_size
    try:
        with Image.open(target, formats=["JPEG", "PNG", "GIF", "WEBP"]) as img:
            frames = getattr(img, "n_frames", 1)
            if frames and frames > 1:
                return {"ok": True, "result": "skipped", "reason": "animated_image"}
            width, height = img.size
            pixels = int(width or 0) * int(height or 0)
            if pixels <= 0:
                return {"ok": False, "result": "failed", "reason": "invalid_dimensions"}
            if pixels > int(max_pixels or 25_000_000):
                return {"ok": True, "result": "skipped", "reason": "image_too_large", "pixels": pixels}

            clean = ImageOps.exif_transpose(img)
            fmt = (img.format or "").upper()
            save_kwargs = {}
            if ext in {".jpg", ".jpeg"}:
                fmt = "JPEG"
                if clean.mode not in {"RGB", "L"}:
                    clean = clean.convert("RGB")
                save_kwargs = {"quality": 92, "optimize": True}
            elif ext == ".png":
                fmt = "PNG"
                save_kwargs = {"optimize": True}
            elif ext == ".gif":
                fmt = "GIF"
            else:
                return {"ok": True, "result": "not_required", "reason": "unsupported_extension"}

            tmp = target.with_suffix(target.suffix + ".reencode.tmp")
            clean.save(tmp, format=fmt, **save_kwargs)
            os.replace(tmp, target)
            after_size = target.stat().st_size
            return {
                "ok": True,
                "result": "clean",
                "reason": "metadata_stripped",
                "format": fmt,
                "pixels": pixels,
                "old_size": before_size,
                "new_size": after_size,
            }
    except Exception as exc:
        try:
            tmp = target.with_suffix(target.suffix + ".reencode.tmp")
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return {"ok": False, "result": "failed", "reason": "image_reencode_failed", "error": exc.__class__.__name__}


def _record_scan_result(conn, *, file_id, scanner_name, result, scanner_version=None, started_at=None, malware_name=None, details=None):
    now = datetime.now().isoformat()
    conn.execute(
        """
        INSERT INTO file_scan_results (
            id, file_id, scanner_name, scanner_version, scan_started_at,
            scan_completed_at, result, malware_name, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uuid.uuid4().hex,
            file_id,
            scanner_name,
            scanner_version,
            started_at or now,
            now,
            result,
            malware_name,
            json.dumps(details or {}, ensure_ascii=False),
            now,
        ),
    )


def _update_file_scan_state(conn, file_id, *, scan_status, risk_level=None):
    fields = ["scan_status=?", "updated_at=?"]
    params = [normalize_scan_status(scan_status), datetime.now().isoformat()]
    if risk_level:
        fields.append("risk_level=?")
        params.append(risk_level)
    params.append(file_id)
    conn.execute(f"UPDATE uploaded_files SET {', '.join(fields)} WHERE id=?", tuple(params))


def _resolve_named_binary(configured, allowed, fallback_order):
    configured = str(configured or "").strip()
    candidates = [configured] if configured else list(fallback_order)
    for name in candidates:
        if name not in allowed:
            continue
        path = shutil.which(name)
        if path:
            return [path]
    return []


def _resolve_clamav_command(policy):
    return _resolve_named_binary(policy.get("scanner_command"), ALLOWED_CLAMAV_COMMANDS, ("clamdscan", "clamscan"))


def _parse_clamav_output(output):
    for line in output.splitlines():
        if " FOUND" in line:
            part = line.rsplit(":", 1)[-1].strip()
            return part.replace(" FOUND", "").strip() or "malware"
    return None


def _resolve_yara_command(policy):
    return _resolve_named_binary(policy.get("yara_command"), ALLOWED_YARA_COMMANDS, ("yara",))


def run_yara_scan(path, *, policy):
    if not policy.get("yara_enabled"):
        return {"result": "not_required", "malware_name": None, "details": {"reason": "yara_disabled"}}
    rules_path = str(policy.get("yara_rules_path") or "").strip()
    if not rules_path:
        return {"result": "not_required", "malware_name": None, "details": {"reason": "yara_rules_not_configured"}}
    ok, reason = _validate_yara_rules_path(rules_path)
    if not ok:
        return {"result": "failed", "malware_name": None, "details": {"reason": "yara_rules_path_not_allowed", "message": reason}}
    command = _resolve_yara_command(policy)
    if not command:
        return {"result": "not_required", "malware_name": None, "details": {"reason": "yara_command_not_found"}}
    started_at = datetime.now().isoformat()
    timeout = int(policy.get("scanner_timeout_seconds") or 60)
    try:
        completed = subprocess.run(
            [*command, "-r", rules_path, str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "result": "failed",
            "malware_name": None,
            "scan_started_at": started_at,
            "details": {"reason": "timeout", "timeout_seconds": timeout, "command": os.path.basename(command[0])},
        }
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    details = {
        "returncode": completed.returncode,
        "command": os.path.basename(command[0]),
        "rules_path": rules_path,
        "output_tail": output[-1000:],
    }
    if completed.returncode not in {0, 1} and not output:
        return {"result": "failed", "malware_name": None, "scan_started_at": started_at, "details": details}
    if completed.stdout.strip():
        rule_name = completed.stdout.strip().split()[0]
        return {"result": "infected", "malware_name": rule_name, "scan_started_at": started_at, "details": details}
    if completed.returncode not in {0, 1}:
        return {"result": "failed", "malware_name": None, "scan_started_at": started_at, "details": details}
    return {"result": "clean", "malware_name": None, "scan_started_at": started_at, "details": details}


def run_clamav_scan(path, *, policy):
    command = _resolve_clamav_command(policy)
    if not command:
        return {
            "result": "not_required",
            "malware_name": None,
            "details": {"reason": "clamav_command_not_found"},
        }
    started_at = datetime.now().isoformat()
    timeout = int(policy.get("scanner_timeout_seconds") or 60)
    try:
        completed = subprocess.run(
            [*command, "--no-summary", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "result": "failed",
            "malware_name": None,
            "scan_started_at": started_at,
            "details": {"reason": "timeout", "timeout_seconds": timeout, "command": os.path.basename(command[0])},
        }
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    details = {
        "returncode": completed.returncode,
        "command": os.path.basename(command[0]),
        "output_tail": output[-1000:],
    }
    if completed.returncode == 0:
        return {"result": "clean", "malware_name": None, "scan_started_at": started_at, "details": details}
    if completed.returncode == 1:
        return {"result": "infected", "malware_name": _parse_clamav_output(output), "scan_started_at": started_at, "details": details}
    return {"result": "failed", "malware_name": None, "scan_started_at": started_at, "details": details}


def scan_archive_members(path, *, policy):
    if not policy.get("deep_archive_scan_enabled"):
        return {"ok": True, "reason": "disabled", "files_scanned": 0, "results": []}
    if not zipfile.is_zipfile(path):
        return {"ok": True, "reason": "not_zip", "files_scanned": 0, "results": []}

    max_files = int(policy.get("max_archive_files") or 200)
    max_bytes = int(policy.get("max_archive_uncompressed_bytes") or 50 * 1024 * 1024)
    max_depth = int(policy.get("max_archive_depth") or 2)
    state = {"files": 0, "bytes": 0, "results": []}

    def scan_one_file(file_path, member_name):
        magic = check_magic_mime_safety(file_path, filename=member_name)
        state["results"].append({"scanner": "archive-member-magic", "member": member_name, **magic})
        if not magic["ok"]:
            return False, "member_magic_mismatch"
        yara = run_yara_scan(file_path, policy=policy)
        if yara["result"] not in {"not_required"}:
            state["results"].append({"scanner": "archive-member-yara", "member": member_name, **yara})
        if yara["result"] == "infected":
            return False, "member_yara_match"
        if yara["result"] == "failed" and policy.get("fail_closed_on_scanner_error"):
            return False, "member_yara_failed"
        if policy.get("scanner_enabled") and policy.get("scanner_backend") == "clamav":
            clamav = run_clamav_scan(file_path, policy=policy)
            if clamav["result"] not in {"not_required"}:
                state["results"].append({"scanner": "archive-member-clamav", "member": member_name, **clamav})
            if clamav["result"] == "infected":
                return False, "member_clamav_infected"
            if clamav["result"] == "failed" and policy.get("fail_closed_on_scanner_error"):
                return False, "member_clamav_failed"
        return True, "ok"

    def walk_zip(zip_blob, depth, base, temp_root):
        with zipfile.ZipFile(zip_blob) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                member = Path(info.filename)
                if member.is_absolute() or ".." in member.parts:
                    return False, "path_traversal"
                state["files"] += 1
                state["bytes"] += int(info.file_size or 0)
                if state["files"] > max_files:
                    return False, "too_many_files"
                if state["bytes"] > max_bytes:
                    return False, "zip_bomb"
                member_name = f"{base}{info.filename}"
                data = archive.read(info)
                if depth < max_depth and file_extension(info.filename) == ".zip":
                    ok, reason = walk_zip(BytesIO(data), depth + 1, f"{member_name}!", temp_root)
                    if not ok:
                        return False, reason
                    continue
                safe_name = uuid.uuid4().hex
                target = Path(temp_root) / safe_name
                target.write_bytes(data)
                ok, reason = scan_one_file(target, member_name)
                if not ok:
                    return False, reason
        return True, "ok"

    try:
        with tempfile.TemporaryDirectory(prefix="upload-scan-") as temp_root:
            ok, reason = walk_zip(path, 0, "", temp_root)
        return {"ok": ok, "reason": reason, "files_scanned": state["files"], "uncompressed_bytes": state["bytes"], "results": state["results"]}
    except zipfile.BadZipFile:
        return {"ok": False, "reason": "bad_zip", "files_scanned": state["files"], "results": state["results"]}
    except Exception as exc:
        return {"ok": False, "reason": "archive_member_scan_failed", "error": exc.__class__.__name__, "files_scanned": state["files"], "results": state["results"]}


def scan_uploaded_file(conn, *, file_id, file_path, filename=None, declared_mime=None):
    ensure_upload_security_schema(conn)
    row = conn.execute("SELECT * FROM uploaded_files WHERE id=?", (file_id,)).fetchone()
    if not row:
        raise ValueError("uploaded file not found")
    policy = get_cloud_drive_security_policy(conn)
    privacy_mode = row["privacy_mode"]
    if is_e2ee_privacy_mode(privacy_mode):
        _record_scan_result(
            conn,
            file_id=file_id,
            scanner_name="server-policy",
            result=row["scan_status"],
            details={"reason": "e2ee_content_not_server_scannable"},
        )
        return {"scan_status": row["scan_status"], "risk_level": row["risk_level"], "results": []}

    path = Path(file_path)
    results = []
    if not path.exists() or not path.is_file():
        _record_scan_result(conn, file_id=file_id, scanner_name="file-presence", result="failed", details={"reason": "missing_file"})
        _update_file_scan_state(conn, file_id, scan_status="failed", risk_level="high" if policy["fail_closed_on_scanner_error"] else None)
        return {"scan_status": "failed", "risk_level": "high" if policy["fail_closed_on_scanner_error"] else row["risk_level"], "results": results}

    if policy["validate_magic_mime"]:
        magic_result = check_magic_mime_safety(path, filename=filename or row["original_filename_plain_for_public"], declared_mime=declared_mime or row["mime_type_plain_for_public"])
        result_name = "clean" if magic_result["ok"] else "infected"
        _record_scan_result(conn, file_id=file_id, scanner_name="magic-mime", result=result_name, details=magic_result)
        results.append({"scanner": "magic-mime", **magic_result})
        if not magic_result["ok"]:
            status = "quarantined" if policy["quarantine_on_infected"] else "infected"
            _update_file_scan_state(conn, file_id, scan_status=status, risk_level="high")
            return {"scan_status": status, "risk_level": "high", "results": results}

    if policy.get("image_reencode_enabled"):
        image_result = reencode_image_strip_metadata(
            path,
            filename=filename or row["original_filename_plain_for_public"],
            max_pixels=policy.get("image_reencode_max_pixels") or 25_000_000,
        )
        scan_result_name = "clean" if image_result.get("ok") else "failed"
        if image_result.get("result") in {"skipped", "not_required"}:
            scan_result_name = "not_required"
        _record_scan_result(conn, file_id=file_id, scanner_name="image-reencode", result=scan_result_name, details=image_result)
        results.append({"scanner": "image-reencode", **image_result})
        if image_result.get("result") == "clean":
            conn.execute(
                "UPDATE uploaded_files SET size_bytes=?, plaintext_sha256=?, updated_at=? WHERE id=?",
                (int(image_result.get("new_size") or path.stat().st_size), sha256_file(path), datetime.now().isoformat(), file_id),
            )

    if policy.get("office_macro_scan_enabled") and file_extension(filename or row["original_filename_plain_for_public"] or path) in OFFICE_EXTENSIONS:
        office_result = check_office_macro_safety(path, filename=filename or row["original_filename_plain_for_public"])
        result_name = "clean" if office_result["ok"] else "infected"
        _record_scan_result(conn, file_id=file_id, scanner_name="office-macro", result=result_name, details=office_result)
        results.append({"scanner": "office-macro", **office_result})
        if not office_result["ok"]:
            status = "quarantined" if policy["quarantine_on_infected"] else "infected"
            _update_file_scan_state(conn, file_id, scan_status=status, risk_level="high")
            return {"scan_status": status, "risk_level": "high", "results": results}

    if file_extension(filename or row["original_filename_plain_for_public"] or path) in ARCHIVE_EXTENSIONS:
        archive_result = check_zip_archive_safety(
            path,
            max_files=policy["max_archive_files"],
            max_uncompressed_bytes=policy["max_archive_uncompressed_bytes"],
            recursive=policy.get("deep_archive_scan_enabled"),
            max_depth=policy.get("max_archive_depth"),
        )
        result_name = "clean" if archive_result["ok"] else "infected"
        _record_scan_result(conn, file_id=file_id, scanner_name="archive-safety", result=result_name, details=archive_result)
        results.append({"scanner": "archive-safety", **archive_result})
        if not archive_result["ok"]:
            status = "quarantined" if policy["quarantine_on_infected"] else "infected"
            _update_file_scan_state(conn, file_id, scan_status=status, risk_level="high")
            return {"scan_status": status, "risk_level": "high", "results": results}
        archive_member_result = scan_archive_members(path, policy=policy)
        member_result_name = "clean" if archive_member_result["ok"] else "infected"
        _record_scan_result(conn, file_id=file_id, scanner_name="archive-member-scan", result=member_result_name, details=archive_member_result)
        results.append({"scanner": "archive-member-scan", **archive_member_result})
        if not archive_member_result["ok"]:
            status = "quarantined" if policy["quarantine_on_infected"] else "infected"
            _update_file_scan_state(conn, file_id, scan_status=status, risk_level="high")
            return {"scan_status": status, "risk_level": "high", "results": results}

    yara = run_yara_scan(path, policy=policy)
    if yara["result"] != "not_required":
        _record_scan_result(
            conn,
            file_id=file_id,
            scanner_name="yara",
            started_at=yara.get("scan_started_at"),
            result=yara["result"],
            malware_name=yara.get("malware_name"),
            details=yara.get("details") or {},
        )
        results.append({"scanner": "yara", **yara})
        if yara["result"] == "infected":
            status = "quarantined" if policy["quarantine_on_infected"] else "infected"
            _update_file_scan_state(conn, file_id, scan_status=status, risk_level="high")
            return {"scan_status": status, "risk_level": "high", "results": results}
        if yara["result"] == "failed" and policy["fail_closed_on_scanner_error"]:
            status = "quarantined"
            _update_file_scan_state(conn, file_id, scan_status=status, risk_level="high")
            return {"scan_status": status, "risk_level": "high", "results": results}

    if not policy["scanner_enabled"] or policy["scanner_backend"] == "disabled":
        _record_scan_result(conn, file_id=file_id, scanner_name="server-policy", result="not_required", details={"reason": "scanner_disabled"})
        _update_file_scan_state(conn, file_id, scan_status="not_required")
        return {"scan_status": "not_required", "risk_level": row["risk_level"], "results": results}

    if policy["scanner_backend"] != "clamav":
        _record_scan_result(conn, file_id=file_id, scanner_name="server-policy", result="failed", details={"reason": "unsupported_scanner_backend", "backend": policy["scanner_backend"]})
        _update_file_scan_state(conn, file_id, scan_status="failed", risk_level="high" if policy["fail_closed_on_scanner_error"] else None)
        return {"scan_status": "failed", "risk_level": "high" if policy["fail_closed_on_scanner_error"] else row["risk_level"], "results": results}

    _update_file_scan_state(conn, file_id, scan_status="scanning")
    clamav = run_clamav_scan(path, policy=policy)
    _record_scan_result(
        conn,
        file_id=file_id,
        scanner_name="clamav",
        scanner_version=None,
        started_at=clamav.get("scan_started_at"),
        result=clamav["result"],
        malware_name=clamav.get("malware_name"),
        details=clamav.get("details") or {},
    )
    results.append({"scanner": "clamav", **clamav})
    if clamav["result"] == "clean":
        _update_file_scan_state(conn, file_id, scan_status="clean")
        return {"scan_status": "clean", "risk_level": row["risk_level"], "results": results}
    if clamav["result"] == "infected":
        status = "quarantined" if policy["quarantine_on_infected"] else "infected"
        _update_file_scan_state(conn, file_id, scan_status=status, risk_level="high")
        return {"scan_status": status, "risk_level": "high", "results": results}
    if clamav["result"] == "not_required":
        _update_file_scan_state(conn, file_id, scan_status="not_required")
        return {"scan_status": "not_required", "risk_level": row["risk_level"], "results": results}

    status = "quarantined" if policy["fail_closed_on_scanner_error"] else "failed"
    _update_file_scan_state(conn, file_id, scan_status=status, risk_level="high" if policy["fail_closed_on_scanner_error"] else None)
    return {"scan_status": status, "risk_level": "high" if policy["fail_closed_on_scanner_error"] else row["risk_level"], "results": results}
