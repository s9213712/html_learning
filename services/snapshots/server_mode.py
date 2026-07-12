"""Server mode profile, audit, and checkpoint service."""

import subprocess
import time

from . import schema as _schema

globals().update(
    {
        name: value
        for name, value in _schema.__dict__.items()
        if not name.startswith("__")
    }
)


class ServerModeService:
    def __init__(self, *, snapshot_service, get_db, get_auth_db=None, get_control_db=None, audit, integrity_guard=None, save_settings=None):
        self.snapshot_service = snapshot_service
        self.get_db = get_db
        self.get_auth_db = get_auth_db or get_db
        self.get_control_db = get_control_db or get_db
        self.audit = audit
        self.integrity_guard = integrity_guard
        self.save_settings = save_settings
        if snapshot_service:
            self.runtime_base_dir = Path(snapshot_service.runtime_base_dir)
        else:
            self.runtime_base_dir = _default_runtime_base_dir()
        self.audit_export_dir = self.runtime_base_dir / "reports" / "server_mode_audit"
        try:
            conn = self.get_control_db()
            try:
                self.ensure_schema(conn)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    def _is_sqlite_locked_error(self, exc):
        return isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc).lower()

    def ensure_schema(self, conn):
        ensure_control_db_schema(conn)

    def _mirror_current_mode_to_main_db(self, *, current_mode, previous_mode=None, checkpoint_id=None, snapshot_id=None, actor_id=None, notes="", reason="", config_json="{}"):
        try:
            conn = self.get_db()
        except Exception:
            return
        try:
            ensure_snapshot_schema(conn)
            conn.execute(
                """
                INSERT INTO server_modes
                (id, current_mode, previous_mode, active_snapshot_id, checkpoint_id, mode_changed_by, mode_changed_at, notes, reason, config_json)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    current_mode=excluded.current_mode,
                    previous_mode=excluded.previous_mode,
                    active_snapshot_id=excluded.active_snapshot_id,
                    checkpoint_id=excluded.checkpoint_id,
                    mode_changed_by=excluded.mode_changed_by,
                    mode_changed_at=excluded.mode_changed_at,
                    notes=excluded.notes,
                    reason=excluded.reason,
                    config_json=excluded.config_json
                """,
                (
                    current_mode,
                    previous_mode,
                    snapshot_id,
                    checkpoint_id,
                    actor_id,
                    datetime.now().isoformat(),
                    notes or "",
                    reason or "",
                    config_json or "{}",
                ),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()

    def _decode_profile(self, row):
        if not row:
            return None
        data = dict(row)
        for key in ("settings_json", "thresholds_json"):
            try:
                data[key.replace("_json", "")] = json.loads(data.get(key) or "{}")
            except Exception:
                data[key.replace("_json", "")] = {}
            data.pop(key, None)
        data["is_builtin"] = bool(data.get("is_builtin"))
        data["color"] = BUILTIN_SECURITY_PROFILES.get(data.get("name"), {}).get("color", "")
        return data

    def _normalize_mode(self, mode):
        value = str(mode or "").strip().lower()
        if value == "preprod":
            return "dev_ready"
        return value

    def _actor_id(self, actor):
        try:
            return int(actor.get("id") if hasattr(actor, "get") else actor["id"])
        except Exception:
            return 0

    def _actor_name(self, actor):
        try:
            return str(actor.get("username") if hasattr(actor, "get") else actor["username"])
        except Exception:
            return "unknown"

    def _actor_role(self, actor):
        try:
            return str(actor.get("role") if hasattr(actor, "get") else actor["role"])
        except Exception:
            return "unknown"

    def _current_mode_for_keys(self):
        try:
            return self._normalize_mode(self.get_current_mode().get("current_mode"))
        except Exception:
            return "test"

    def _record_security_key(self, *, purpose, key_version, status):
        conn = self.get_control_db()
        try:
            self.ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO security_keys (purpose, key_version, created_at, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(purpose, key_version) DO UPDATE SET status=excluded.status
                """,
                (purpose, key_version, datetime.now().isoformat(), status),
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        finally:
            conn.close()

    def _record_security_key_on_conn(self, conn, *, purpose, key_version, status):
        conn.execute(
            """
            INSERT INTO security_keys (purpose, key_version, created_at, status)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(purpose, key_version) DO UPDATE SET status=excluded.status
            """,
            (purpose, key_version, datetime.now().isoformat(), status),
        )

    def _local_hmac_key_path(self, purpose):
        filename = ".server_mode_log_hmac_key" if purpose == "server_mode_log" else f".{purpose}_hmac_key"
        return self.runtime_base_dir / filename

    def _hmac_key(self, purpose="server_mode_log", current_mode=None):
        purpose_env = {
            "server_mode_log": ("SERVER_MODE_LOG_HMAC_KEY", "SERVER_MODE_LOG_HMAC_KEY_VERSION"),
            "server_mode_token": ("SERVER_MODE_TOKEN_HMAC_KEY", "SERVER_MODE_TOKEN_HMAC_KEY_VERSION"),
            "server_mode_report": ("SERVER_MODE_REPORT_HMAC_KEY", "SERVER_MODE_REPORT_HMAC_KEY_VERSION"),
        }
        env_name, version_env = purpose_env.get(purpose, ("SERVER_MODE_TOKEN_HMAC_KEY", "SERVER_MODE_TOKEN_HMAC_KEY_VERSION"))
        key = os.environ.get(env_name, "").strip()
        version = os.environ.get(version_env, "env-v1").strip() or "env-v1"
        if key:
            return key, version
        mode_for_key_policy = self._normalize_mode(current_mode) if current_mode else self._current_mode_for_keys()
        production_key_required = (
            os.environ.get("HTML_LEARNING_REQUIRE_EXTERNAL_HMAC_KEYS")
            or os.environ.get("HTML_LEARNING_ENV", "").lower() in {"prod", "production"}
        )
        if mode_for_key_policy == "production" and production_key_required and not os.environ.get("HTML_LEARNING_ALLOW_LOCAL_SERVER_MODE_KEYS"):
            raise RuntimeError(f"{env_name} is required in production")
        path = self._local_hmac_key_path(purpose)
        if path.exists():
            key = path.read_text(encoding="utf-8").strip()
        else:
            key = secrets.token_urlsafe(48)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(key + "\n", encoding="utf-8")
            try:
                path.chmod(0o600)
            except Exception:
                pass
        version = "local-dev-v1"
        return key, version

    def _sign_mode_log(self, row):
        key, version = self._hmac_key("server_mode_log")
        payload = {**row, "key_version": version}
        return _hmac_sha256(key, _mode_switch_signature_payload(payload)), version

    def _verify_mode_log_signature(self, row):
        signature = str(row.get("hmac_signature") or "")
        if not signature:
            return {"ok": False, "reason": "missing_signature", "key_version": row.get("key_version") or ""}
        try:
            key, _ = self._hmac_key("server_mode_log")
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "key_version": row.get("key_version") or ""}
        expected = _hmac_sha256(key, _mode_switch_signature_payload(row))
        ok = hmac.compare_digest(signature, expected)
        return {"ok": ok, "reason": "" if ok else "signature_mismatch", "key_version": row.get("key_version") or ""}

    def _export_mode_log_event(self, row):
        self.audit_export_dir.mkdir(parents=True, exist_ok=True)
        event_uuid = row.get("event_uuid") or row.get("id")
        timestamp = str(row.get("created_at") or datetime.now().isoformat()).replace(":", "").replace("-", "")
        payload = {
            "event": row,
            "row_hash": row.get("row_hash"),
            "prev_hash": row.get("prev_hash"),
            "hmac_signature": row.get("hmac_signature"),
            "key_version": row.get("key_version"),
        }
        event_path = self.audit_export_dir / f"{timestamp}_{event_uuid}.json"
        event_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        day = datetime.now().strftime("%Y%m%d")
        bundle = self.audit_export_dir / f"server_mode_audit_{day}.jsonl"
        with bundle.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        (self.audit_export_dir / f"server_mode_audit_{day}.sha256").write_text(f"{digest}  {bundle.name}\n", encoding="utf-8")
        return {"event_path": str(event_path), "bundle": str(bundle), "sha256": digest}

    def _prepare_production_report_attestation(
        self,
        *,
        report_type,
        raw_report,
        target_commit="",
        target_branch="",
        server_mode="",
        test_result="",
        passed=False,
        critical_findings_count=0,
        high_findings_count=0,
        unresolved_findings=None,
        tester="",
        report_source="manual_signed_upload",
    ):
        if raw_report is None:
            return {"ok": False, "reason": "missing_raw_report"}
        raw_report_json = _canonical_json_text(raw_report)
        report_hash = f"sha256:{hashlib.sha256(raw_report_json.encode('utf-8')).hexdigest()}"
        key, key_version = self._hmac_key("server_mode_report")
        unresolved_json = _canonical_json_text(list(unresolved_findings or []))
        payload = {
            "report_type": str(report_type or "").strip(),
            "report_hash": report_hash,
            "target_commit": str(target_commit or "").strip(),
            "target_branch": str(target_branch or "").strip(),
            "server_mode": str(server_mode or "").strip(),
            "test_result": str(test_result or "").strip().lower(),
            "pass": 1 if passed else 0,
            "critical_findings_count": int(critical_findings_count or 0),
            "high_findings_count": int(high_findings_count or 0),
            "unresolved_findings_json": unresolved_json,
            "tester": str(tester or "").strip(),
            "raw_report_json": raw_report_json,
            "report_source": str(report_source or "manual_signed_upload").strip() or "manual_signed_upload",
            "key_version": key_version,
        }
        signature = f"hmac_sha256:{_hmac_sha256(key, _production_report_signature_payload(payload))}"
        return {
            "ok": True,
            "report_hash": report_hash,
            "signature": signature,
            "key_version": key_version,
            "raw_report_json": raw_report_json,
            "payload": payload,
        }

    def _verify_production_report_signature(self, row):
        raw_report_json = str(row.get("raw_report_json") or "").strip()
        if not raw_report_json:
            return {"ok": False, "reason": "missing_raw_report_json"}
        try:
            raw_report = json.loads(raw_report_json)
        except Exception:
            return {"ok": False, "reason": "invalid_raw_report_json"}
        normalized_raw_report_json = _canonical_json_text(raw_report)
        expected_hash = f"sha256:{hashlib.sha256(normalized_raw_report_json.encode('utf-8')).hexdigest()}"
        if str(row.get("report_hash") or "").strip() != expected_hash:
            return {"ok": False, "reason": "report_hash_mismatch"}
        signature = str(row.get("signature") or "").strip()
        if not signature:
            return {"ok": False, "reason": "missing_signature"}
        if not signature.startswith("hmac_sha256:"):
            return {"ok": False, "reason": "unsupported_signature_scheme"}
        try:
            key, key_version = self._hmac_key(
                "server_mode_report",
                current_mode=str(row.get("server_mode") or "").strip(),
            )
        except Exception as exc:
            return {"ok": False, "reason": str(exc)}
        stored_key_version = str(row.get("key_version") or "").strip()
        if stored_key_version and stored_key_version != key_version:
            return {"ok": False, "reason": "key_version_mismatch", "expected_key_version": key_version}
        payload = {
            "report_type": str(row.get("report_type") or "").strip(),
            "report_hash": expected_hash,
            "target_commit": str(row.get("target_commit") or "").strip(),
            "target_branch": str(row.get("target_branch") or "").strip(),
            "server_mode": str(row.get("server_mode") or "").strip(),
            "test_result": str(row.get("test_result") or "").strip().lower(),
            "pass": int(row.get("pass") or 0),
            "critical_findings_count": int(row.get("critical_findings_count") or 0),
            "high_findings_count": int(row.get("high_findings_count") or 0),
            "unresolved_findings_json": str(row.get("unresolved_findings_json") or "[]"),
            "tester": str(row.get("tester") or "").strip(),
            "raw_report_json": normalized_raw_report_json,
            "report_source": str(row.get("report_source") or "manual_signed_upload").strip() or "manual_signed_upload",
            "key_version": stored_key_version or key_version,
        }
        expected_signature = f"hmac_sha256:{_hmac_sha256(key, _production_report_signature_payload(payload))}"
        return {"ok": hmac.compare_digest(signature, expected_signature), "reason": "" if hmac.compare_digest(signature, expected_signature) else "signature_mismatch", "key_version": stored_key_version or key_version}

    def _production_gate_reports_dir(self):
        return self.runtime_base_dir / "reports" / "security" / "production_gate"

    def _current_production_target(self, conn=None):
        repo_dir = os.environ.get("HTML_LEARNING_GIT_REPO_DIR", "").strip()
        branch = ""
        commit = ""
        if repo_dir:
            try:
                branch = subprocess.check_output(
                    ["git", "-C", repo_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    text=True,
                ).strip()
            except Exception:
                branch = ""
            try:
                commit = subprocess.check_output(
                    ["git", "-C", repo_dir, "rev-parse", "HEAD"],
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    text=True,
                ).strip()
            except Exception:
                commit = ""
        current_mode = "dev_ready"
        report_server_mode = "dev_ready"
        try:
            if conn is not None:
                row = conn.execute("SELECT current_mode, previous_mode FROM server_modes WHERE id=1").fetchone()
                previous_mode = self._normalize_mode(row["previous_mode"] if row else "")
                current_mode = self._normalize_mode(row["current_mode"] if row else current_mode)
            else:
                row = self.get_current_mode()
                previous_mode = self._normalize_mode(row.get("previous_mode"))
                current_mode = self._normalize_mode(row.get("current_mode"))
        except Exception:
            current_mode = "dev_ready"
            previous_mode = ""
        if current_mode == "production" and previous_mode and previous_mode != "production":
            report_server_mode = previous_mode
        else:
            report_server_mode = current_mode
        return {
            "target_commit": commit,
            "target_branch": branch,
            "server_mode": report_server_mode,
            "current_mode": current_mode,
            "report_server_mode": report_server_mode,
        }

    def _report_matches_current_target(self, row, current_target):
        if not row:
            return False, "missing_report"
        expected_commit = str((current_target or {}).get("target_commit") or "").strip()
        expected_branch = str((current_target or {}).get("target_branch") or "").strip()
        expected_mode = self._normalize_mode((current_target or {}).get("server_mode"))
        actual_commit = str(row.get("target_commit") or "").strip()
        actual_branch = str(row.get("target_branch") or "").strip()
        actual_mode = self._normalize_mode(row.get("server_mode"))
        if expected_commit and actual_commit != expected_commit:
            return False, "target_commit_mismatch"
        if expected_branch and actual_branch != expected_branch:
            return False, "target_branch_mismatch"
        if expected_mode and actual_mode != expected_mode:
            return False, "server_mode_mismatch"
        return True, ""

    def _normalize_production_report_record(self, row, *, expected_report_type=None, current_target=None):
        if not row:
            return None
        item = dict(row)
        if item.get("verification_reason") == "invalid_report_json" and not bool(item.get("signature_valid")):
            item["trust_level"] = "unverified"
            item["target_match"], item["target_verification_reason"] = self._report_matches_current_target(
                item,
                current_target or self._current_production_target(),
            )
            return item
        if expected_report_type and str(item.get("report_type") or "").strip() != str(expected_report_type).strip():
            item["signature_valid"] = False
            item["verification_reason"] = "report_type_mismatch"
            item["trust_level"] = "unverified"
        else:
            sig = self._verify_production_report_signature(item)
            item["signature_valid"] = bool(sig.get("ok"))
            item["verification_reason"] = sig.get("reason") or ""
            item["trust_level"] = "verified" if sig.get("ok") else "unverified"
        target_match, target_reason = self._report_matches_current_target(item, current_target or self._current_production_target())
        item["target_match"] = bool(target_match)
        item["target_verification_reason"] = target_reason
        return item

    def _latest_production_report_file_record(self, report_type):
        report_path = self._production_gate_reports_dir() / f"{report_type}_report.json"
        if not report_path.exists():
            return None
        created_at = datetime.fromtimestamp(report_path.stat().st_mtime).isoformat()
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            return {
                "id": f"file:{report_type}",
                "report_type": report_type,
                "report_hash": "",
                "target_commit": "",
                "target_branch": "",
                "server_mode": "",
                "test_result": "",
                "pass": 0,
                "critical_findings_count": 0,
                "high_findings_count": 1,
                "unresolved_findings_json": "[]",
                "tester": "filesystem_auto_detect",
                "signature": "",
                "raw_report_json": "{}",
                "report_source": "filesystem_auto_detect",
                "trust_level": "unverified",
                "key_version": "",
                "verified_at": "",
                "created_at": created_at,
                "canonical_path": str(report_path),
                "signature_valid": False,
                "verification_reason": "invalid_report_json",
            }
        record = {
            "id": f"file:{report_type}",
            "report_type": str(payload.get("report_type") or report_type).strip(),
            "report_hash": str(payload.get("report_hash") or "").strip(),
            "target_commit": str(payload.get("target_commit") or "").strip(),
            "target_branch": str(payload.get("target_branch") or "").strip(),
            "server_mode": str(payload.get("server_mode") or "").strip(),
            "test_result": str(payload.get("test_result") or "").strip(),
            "pass": 1 if bool(payload.get("pass") if "pass" in payload else payload.get("passed")) else 0,
            "critical_findings_count": int(payload.get("critical_findings_count") or 0),
            "high_findings_count": int(payload.get("high_findings_count") or 0),
            "unresolved_findings_json": json.dumps(payload.get("unresolved_findings") or [], ensure_ascii=False, sort_keys=True),
            "tester": str(payload.get("tester") or "filesystem_auto_detect").strip() or "filesystem_auto_detect",
            "signature": str(payload.get("signature") or "").strip(),
            "raw_report_json": _canonical_json_text(payload.get("raw_report") or {}),
            "report_source": str(payload.get("report_source") or "filesystem_auto_detect").strip() or "filesystem_auto_detect",
            "trust_level": "unverified",
            "key_version": str(payload.get("key_version") or "").strip(),
            "verified_at": created_at,
            "created_at": created_at,
            "canonical_path": str(report_path),
        }
        return self._normalize_production_report_record(record, expected_report_type=report_type)

    def _prefer_newer_production_report_record(self, current_row, file_row, *, current_target=None):
        if file_row is None:
            return current_row
        if current_row is None:
            return file_row
        current_target = current_target or self._current_production_target()
        current_verified = str(current_row.get("trust_level") or "").strip() == "verified" and bool(current_row.get("signature_valid"))
        file_verified = str(file_row.get("trust_level") or "").strip() == "verified" and bool(file_row.get("signature_valid"))
        if current_verified and not file_verified:
            return current_row
        if file_verified and not current_verified:
            return file_row
        current_target_match = bool(current_row.get("target_match"))
        file_target_match = bool(file_row.get("target_match"))
        if current_verified and file_verified:
            same_target = (
                str(current_row.get("target_commit") or "").strip() == str(file_row.get("target_commit") or "").strip()
                and str(current_row.get("target_branch") or "").strip() == str(file_row.get("target_branch") or "").strip()
                and self._normalize_mode(current_row.get("server_mode")) == self._normalize_mode(file_row.get("server_mode"))
            )
            if current_target_match and not file_target_match:
                return current_row
            if file_target_match and not current_target_match:
                return file_row
            if not same_target:
                return current_row
        try:
            current_created = datetime.fromisoformat(str(current_row.get("created_at") or ""))
        except Exception:
            current_created = datetime.min
        try:
            file_created = datetime.fromisoformat(str(file_row.get("created_at") or ""))
        except Exception:
            file_created = datetime.min
        if current_verified and file_verified:
            return file_row if same_target and file_target_match and file_created > current_created else current_row
        return file_row if file_created >= current_created else current_row

    def _stable_hash(self, payload):
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()

    def _table_exists(self, conn, table):
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return bool(row)

    def _settings_snapshot(self, conn):
        if not self._table_exists(conn, "system_settings"):
            return {}
        rows = conn.execute("SELECT key, value FROM system_settings ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def _points_chain_checkpoint(self, conn):
        payload = {"ledger_count": 0, "block_count": 0, "latest_block_hash": "", "latest_ledger_hash": ""}
        if self._table_exists(conn, "points_ledger"):
            try:
                row = conn.execute("SELECT COUNT(*) AS c FROM points_ledger").fetchone()
                payload["ledger_count"] = int(row["c"] or 0)
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(points_ledger)").fetchall()}
                if "entry_hash" in cols:
                    latest = conn.execute("SELECT entry_hash FROM points_ledger ORDER BY id DESC LIMIT 1").fetchone()
                    payload["latest_ledger_hash"] = latest["entry_hash"] if latest else ""
            except Exception as exc:
                payload["ledger_error"] = str(exc)
        if self._table_exists(conn, "points_chain_blocks"):
            try:
                row = conn.execute("SELECT COUNT(*) AS c FROM points_chain_blocks").fetchone()
                payload["block_count"] = int(row["c"] or 0)
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(points_chain_blocks)").fetchall()}
                hash_col = "block_hash" if "block_hash" in cols else ("hash" if "hash" in cols else "")
                if hash_col:
                    latest = conn.execute(f"SELECT {hash_col} AS h FROM points_chain_blocks ORDER BY id DESC LIMIT 1").fetchone()
                    payload["latest_block_hash"] = latest["h"] if latest else ""
            except Exception as exc:
                payload["block_error"] = str(exc)
        payload["hash"] = self._stable_hash(payload)
        return payload

    def _cloud_drive_metadata_checkpoint(self, conn):
        tables = ["storage_files", "storage_folders", "storage_share_links", "cloud_file_refs", "uploaded_files", "videos"]
        payload = {}
        for table in tables:
            if not self._table_exists(conn, table):
                payload[table] = {"exists": False, "count": 0}
                continue
            try:
                count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
                payload[table] = {"exists": True, "count": int(count or 0)}
            except Exception as exc:
                payload[table] = {"exists": True, "error": str(exc)}
        payload["hash"] = self._stable_hash(payload)
        return payload

    def _integrity_manifest_hash(self):
        path = getattr(self.integrity_guard, "manifest_path", None) if self.integrity_guard else None
        if not path:
            return ""
        try:
            path_obj = Path(path)
            return _sha256_file(path_obj) if path_obj.is_file() else ""
        except Exception:
            return ""

    def _config_diff(self, current_settings, target_settings):
        diff = {}
        for key, after in (target_settings or {}).items():
            before = current_settings.get(key)
            if str(before) != str(after):
                diff[key] = {"before": before, "after": after}
        return diff

    def _record_mode_switch(
        self,
        conn,
        *,
        from_mode,
        to_mode,
        actor,
        reason="",
        checkpoint_id=None,
        snapshot_id=None,
        success=False,
        error_message="",
        config_diff=None,
        restore_result=None,
        source_ip="",
        user_agent="",
        request_id="",
    ):
        log_id = f"mode_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
        event_uuid = secrets.token_hex(16)
        created_at = datetime.now().isoformat()
        prev_row = conn.execute(
            "SELECT row_hash FROM mode_switch_logs ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        prev_hash = (prev_row["row_hash"] if prev_row and prev_row["row_hash"] else "") if prev_row else ""
        row_payload = {
            "id": log_id,
            "event_uuid": event_uuid,
            "from_mode": from_mode,
            "to_mode": to_mode,
            "actor_user_id": self._actor_id(actor),
            "actor_id": self._actor_id(actor),
            "actor_role": self._actor_role(actor),
            "source_ip": source_ip or "",
            "user_agent": user_agent or "",
            "request_id": request_id or "",
            "reason": reason or "",
            "checkpoint_id": checkpoint_id,
            "snapshot_id": snapshot_id,
            "success": 1 if success else 0,
            "error_message": error_message or "",
            "config_diff_json": json.dumps(config_diff or {}, ensure_ascii=False, sort_keys=True),
            "restore_result_json": json.dumps(restore_result or {}, ensure_ascii=False, sort_keys=True),
            "created_at": created_at,
            "server_boot_id": SERVER_BOOT_ID,
        }
        hmac_key, key_version = self._hmac_key("server_mode_log", current_mode=to_mode)
        row_payload["key_version"] = key_version
        row_hash = _mode_switch_log_hash(row_payload, prev_hash)
        row_payload["prev_hash"] = prev_hash
        row_payload["row_hash"] = row_hash
        hmac_signature = _hmac_sha256(hmac_key, _mode_switch_signature_payload(row_payload))
        row_payload["hmac_signature"] = hmac_signature
        self._record_security_key_on_conn(
            conn,
            purpose="server_mode_log",
            key_version=key_version,
            status="active",
        )
        conn.execute(
            """
            INSERT INTO mode_switch_logs
            (id, event_uuid, from_mode, to_mode, actor_user_id, actor_id, actor_role, source_ip, user_agent, request_id,
             reason, checkpoint_id, snapshot_id, success, error_message, config_diff_json, restore_result_json,
             created_at, prev_hash, row_hash, server_boot_id, hmac_signature, key_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                event_uuid,
                from_mode,
                to_mode,
                row_payload["actor_user_id"],
                row_payload["actor_id"],
                row_payload["actor_role"],
                row_payload["source_ip"],
                row_payload["user_agent"],
                row_payload["request_id"],
                reason or "",
                checkpoint_id,
                snapshot_id,
                1 if success else 0,
                error_message or "",
                row_payload["config_diff_json"],
                row_payload["restore_result_json"],
                created_at,
                prev_hash,
                row_hash,
                SERVER_BOOT_ID,
                hmac_signature,
                key_version,
            ),
        )
        try:
            export = self._export_mode_log_event(row_payload)
            if not export.get("event_path"):
                raise RuntimeError("empty export path")
        except Exception as exc:
            if self._normalize_mode(to_mode) in {"production", "dev_ready"}:
                raise RuntimeError(f"mode switch audit export failed: {exc}") from exc
        return log_id

    def _enter_incident_lockdown_on_conn(self, conn, *, actor, trigger_type, reason, verification=None):
        now = datetime.now().isoformat()
        current_row = conn.execute("SELECT current_mode FROM server_modes WHERE id=1").fetchone()
        current = current_row["current_mode"] if current_row else None
        incident_id = f"incident_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
        conn.execute(
            """
            INSERT INTO incident_reports
            (id, status, trigger_type, reason, entered_by, entered_at, verification_json)
            VALUES (?, 'open', ?, ?, ?, ?, ?)
            """,
            (
                incident_id,
                str(trigger_type or "manual"),
                str(reason or ""),
                self._actor_id(actor),
                now,
                json.dumps(verification or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        profile = BUILTIN_SECURITY_PROFILES["incident_lockdown"]
        now_updated_by = f"server_mode:{self._actor_name(actor)}"
        if self._table_exists(conn, "system_settings"):
            try:
                epoch_row = conn.execute("SELECT value FROM system_settings WHERE key='server_security_epoch'").fetchone()
                next_epoch = int((epoch_row["value"] if epoch_row else 0) or 0) + 1
            except Exception:
                next_epoch = 1
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                ("server_security_epoch", str(next_epoch), now, now_updated_by),
            )
            for key, value in (profile.get("settings") or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO system_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                    (key, str(value), now, now_updated_by),
                )
        if self._table_exists(conn, "tester_tokens"):
            conn.execute(
                "UPDATE tester_tokens SET revoked_at=? WHERE revoked_at IS NULL",
                (now,),
            )
        conn.execute(
            """
            UPDATE server_modes
            SET previous_mode=?, current_mode='incident_lockdown', checkpoint_id=NULL, active_snapshot_id=NULL,
                mode_changed_by=?, mode_changed_at=?, notes=?, reason=?, config_json=?
            WHERE id=1
            """,
            (
                current,
                self._actor_id(actor),
                now,
                reason or "",
                reason or "",
                json.dumps(profile, ensure_ascii=False, sort_keys=True),
            ),
        )
        self._record_mode_switch(
            conn,
            from_mode=current,
            to_mode="incident_lockdown",
            actor=actor,
            reason=reason or trigger_type or "",
            success=True,
            config_diff={"trigger_type": trigger_type, "verification": verification or {}},
        )
        return incident_id

    def create_mode_checkpoint(self, *, actor, target_mode, reason="", snapshot_type="mode_checkpoint", from_mode=None):
        target_mode = self._normalize_mode(target_mode)
        if target_mode not in SERVER_MODES and not self.get_profile(target_mode):
            return {"ok": False, "msg": "server mode 錯誤"}
        if not self.snapshot_service:
            return {"ok": False, "msg": "Snapshot 服務目前無法使用"}
        snapshot = self.snapshot_service.create_snapshot(
            snapshot_type=snapshot_type,
            actor=actor,
            notes=f"server mode checkpoint before {target_mode}: {reason or ''}",
        )
        checkpoint_id = f"chk_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
        main_conn = self.get_db()
        control_conn = self.get_control_db()
        try:
            self.ensure_schema(control_conn)
            current_settings = self._settings_snapshot(main_conn)
            security_settings = {
                key: current_settings.get(key)
                for key in sorted(current_settings)
                if key.startswith("feature_")
                or key in {
                    "audit_chain_enabled",
                    "ip_blocking_enabled",
                    "login_violation_enabled",
                    "rate_limit_violation_enabled",
                    "integrity_guard_enabled",
                    "integrity_guard_strict_mode",
                    "maintenance_mode",
                    "captcha_mode",
                }
            }
            points = self._points_chain_checkpoint(main_conn)
            cloud = self._cloud_drive_metadata_checkpoint(main_conn)
            integrity_hash = self._integrity_manifest_hash()
            db_hash = ""
            try:
                if snapshot.ok and snapshot.snapshot_id:
                    snapshot_row = main_conn.execute(
                        "SELECT db_dump_path FROM snapshots WHERE id=?",
                        (snapshot.snapshot_id,),
                    ).fetchone()
                    db_dump_path = Path(snapshot_row["db_dump_path"] if snapshot_row else "")
                    if db_dump_path.is_file():
                        db_hash = _sha256_file(db_dump_path)
            except Exception:
                db_hash = ""
            components = {
                "db_snapshot": {"snapshot_id": snapshot.snapshot_id, "hash": db_hash},
                "config": current_settings,
                "security_settings": security_settings,
                "points_chain": points,
                "cloud_drive_metadata": cloud,
                "integrity_manifest": {"hash": integrity_hash},
            }
            control_conn.execute(
                """
                INSERT INTO server_checkpoints
                (id, snapshot_id, checkpoint_type, from_mode, target_mode, created_by, created_at, status,
                 db_snapshot_hash, config_hash, security_settings_hash, points_chain_hash,
                 cloud_drive_metadata_hash, integrity_manifest_hash, components_json, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    snapshot.snapshot_id,
                    snapshot_type,
                    from_mode,
                    target_mode,
                    self._actor_id(actor),
                    datetime.now().isoformat(),
                    "ready" if snapshot.ok else "failed",
                    db_hash,
                    self._stable_hash(current_settings),
                    self._stable_hash(security_settings),
                    points.get("hash", ""),
                    cloud.get("hash", ""),
                    integrity_hash,
                    json.dumps(components, ensure_ascii=False, sort_keys=True),
                    snapshot.error if not snapshot.ok else "",
                ),
            )
            control_conn.commit()
        finally:
            main_conn.close()
            control_conn.close()
        if not snapshot.ok:
            return {"ok": False, "msg": "mode checkpoint snapshot 建立失敗", "checkpoint_id": checkpoint_id, "error": snapshot.error}
        return {
            "ok": True,
            "checkpoint_id": checkpoint_id,
            "snapshot_id": snapshot.snapshot_id,
            "components": components,
        }

    def _checkpoint_record(self, conn, checkpoint_id):
        row = conn.execute("SELECT * FROM server_checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        return dict(row) if row else None

    def validate_checkpoint_restore(self, *, checkpoint_id, expected_checkpoint=None):
        control_conn = self.get_control_db()
        main_conn = self.get_db()
        try:
            self.ensure_schema(control_conn)
            checkpoint = expected_checkpoint or self._checkpoint_record(control_conn, checkpoint_id)
            if not checkpoint:
                return {"ok": False, "msg": "找不到 checkpoint", "checkpoint_id": checkpoint_id}
            try:
                components = json.loads(checkpoint.get("components_json") or "{}")
            except Exception:
                components = {}
            snapshot_id = checkpoint.get("snapshot_id")
            snapshot_verification = {"ok": False, "msg": "Snapshot 服務目前無法使用"}
            if snapshot_id:
                try:
                    snapshot_verification = self.snapshot_service.verify_snapshot(snapshot_id=snapshot_id)
                except Exception as exc:
                    snapshot_verification = {"ok": False, "msg": str(exc)}

            current_settings = self._settings_snapshot(main_conn)
            security_settings = {
                key: current_settings.get(key)
                for key in sorted(current_settings)
                if key.startswith("feature_")
                or key in {
                    "audit_chain_enabled",
                    "ip_blocking_enabled",
                    "login_violation_enabled",
                    "rate_limit_violation_enabled",
                    "integrity_guard_enabled",
                    "integrity_guard_strict_mode",
                    "maintenance_mode",
                    "captcha_mode",
                }
            }
            current = {
                "config_hash": self._stable_hash(current_settings),
                "security_settings_hash": self._stable_hash(security_settings),
                "points_chain_hash": self._points_chain_checkpoint(main_conn).get("hash", ""),
                "cloud_drive_metadata_hash": self._cloud_drive_metadata_checkpoint(main_conn).get("hash", ""),
                "integrity_manifest_hash": self._integrity_manifest_hash(),
            }
            checks = {
                "snapshot_verified": bool(snapshot_verification.get("ok")),
                "config": current["config_hash"] == checkpoint.get("config_hash"),
                "security_settings": current["security_settings_hash"] == checkpoint.get("security_settings_hash"),
                "points_chain": current["points_chain_hash"] == checkpoint.get("points_chain_hash"),
                "cloud_drive_metadata": current["cloud_drive_metadata_hash"] == checkpoint.get("cloud_drive_metadata_hash"),
                "integrity_manifest": current["integrity_manifest_hash"] == (checkpoint.get("integrity_manifest_hash") or ""),
            }
            mismatches = [name for name, ok in checks.items() if not ok]
            return {
                "ok": not mismatches,
                "checkpoint_id": checkpoint_id,
                "snapshot_id": snapshot_id,
                "checks": checks,
                "mismatches": mismatches,
                "snapshot_verification": snapshot_verification,
                "expected": {
                    "config_hash": checkpoint.get("config_hash"),
                    "security_settings_hash": checkpoint.get("security_settings_hash"),
                    "points_chain_hash": checkpoint.get("points_chain_hash"),
                    "cloud_drive_metadata_hash": checkpoint.get("cloud_drive_metadata_hash"),
                    "integrity_manifest_hash": checkpoint.get("integrity_manifest_hash"),
                    "components": components,
                },
                "current": current,
            }
        finally:
            control_conn.close()
            main_conn.close()

    def list_profiles(self):
        conn = self.get_control_db()
        try:
            rows = conn.execute(
                "SELECT * FROM security_profiles ORDER BY is_builtin DESC, name"
            ).fetchall()
            return [self._decode_profile(row) for row in rows]
        finally:
            conn.close()

    def get_profile(self, name):
        profile_name = self._normalize_mode(name)
        conn = self.get_control_db()
        try:
            row = conn.execute("SELECT * FROM security_profiles WHERE name=?", (profile_name,)).fetchone()
            return self._decode_profile(row)
        finally:
            conn.close()

    def save_profile(self, *, name, label, description="", settings=None, thresholds=None, actor=None):
        profile_name = str(name or "").strip().lower()
        if not PROFILE_NAME_RE.fullmatch(profile_name):
            return {"ok": False, "msg": "profile name 必須是 2-32 字元的小寫英數、底線或連字號，且以英文字母開頭"}
        if profile_name in SERVER_MODES:
            return {"ok": False, "msg": "內建模式不可覆寫，請使用自定義名稱"}
        settings = settings if isinstance(settings, dict) else {}
        thresholds = thresholds if isinstance(thresholds, dict) else {}
        try:
            actor_id = int(actor["id"] if actor else 0)
        except Exception:
            actor_id = int(actor.get("id") or 0) if hasattr(actor, "get") else 0
        now = datetime.now().isoformat()
        conn = self.get_control_db()
        try:
            conn.execute(
                """
                INSERT INTO security_profiles
                (name, label, description, settings_json, thresholds_json, is_builtin, created_by, updated_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    label=excluded.label,
                    description=excluded.description,
                    settings_json=excluded.settings_json,
                    thresholds_json=excluded.thresholds_json,
                    updated_by=excluded.updated_by,
                    updated_at=excluded.updated_at
                """,
                (
                    profile_name,
                    str(label or profile_name)[:80],
                    str(description or "")[:500],
                    json.dumps(settings, ensure_ascii=False, sort_keys=True),
                    json.dumps(thresholds, ensure_ascii=False, sort_keys=True),
                    actor_id,
                    actor_id,
                    now,
                    now,
                ),
            )
            conn.commit()
            profile = conn.execute("SELECT * FROM security_profiles WHERE name=?", (profile_name,)).fetchone()
            return {"ok": True, "profile": self._decode_profile(profile)}
        finally:
            conn.close()

    def get_current_mode(self):
        for attempt in range(5):
            conn = self.get_control_db()
            try:
                row = conn.execute("SELECT * FROM server_modes WHERE id=1").fetchone()
                return dict(row)
            except Exception as exc:
                try:
                    conn.rollback()
                except Exception:
                    pass
                if attempt >= 4 or not self._is_sqlite_locked_error(exc):
                    raise
                time.sleep(0.15)
            finally:
                conn.close()

    def mode_switch_logs(self, *, limit=50):
        limit = max(1, min(int(limit or 50), 200))
        conn = self.get_control_db()
        try:
            rows = conn.execute(
                "SELECT * FROM mode_switch_logs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def verify_mode_switch_logs(self):
        conn = self.get_control_db()
        try:
            chain = verify_mode_switch_log_hash_chain(conn)
            rows = conn.execute(
                "SELECT * FROM mode_switch_logs ORDER BY created_at ASC, id ASC"
            ).fetchall()
            invalid = []
            for row in rows:
                item = dict(row)
                sig = self._verify_mode_log_signature(item)
                if not sig.get("ok"):
                    invalid.append({"id": item.get("id"), "event_uuid": item.get("event_uuid"), **sig})
            return {
                **chain,
                "chain_length": chain.get("count", 0),
                "broken_links": len(chain.get("mismatches") or []),
                "invalid_signatures": invalid,
                "first_hash": rows[0]["row_hash"] if rows else "",
                "last_hash": chain.get("latest_hash") or "",
                "result": "PASS" if chain.get("ok") and not invalid else "FAIL",
                "ok": bool(chain.get("ok") and not invalid),
            }
        finally:
            conn.close()

    def _production_requirements_on_conn(self, conn):
        reports = {}
        current_target = self._current_production_target(conn)
        for report_type in PRODUCTION_REQUIRED_REPORT_TYPES:
            row = conn.execute(
                """
                SELECT * FROM production_entry_reports
                WHERE report_type=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (report_type,),
            ).fetchone()
            current_row = self._normalize_production_report_record(
                row,
                expected_report_type=report_type,
                current_target=current_target,
            ) if row else None
            file_row = self._latest_production_report_file_record(report_type)
            if file_row:
                file_row = self._normalize_production_report_record(
                    file_row,
                    expected_report_type=report_type,
                    current_target=current_target,
                )
            reports[report_type] = self._prefer_newer_production_report_record(
                current_row,
                file_row,
                current_target=current_target,
            )
        missing = [key for key, row in reports.items() if not row]
        failed = [
            key
            for key, row in reports.items()
            if row
            and (
                not bool(row["pass"])
                or int(row["critical_findings_count"] or 0) > 0
                or int(row["high_findings_count"] or 0) > 0
                or not row["report_hash"]
                or str(row.get("trust_level") or "").strip() != "verified"
                or not bool(row.get("signature_valid"))
                or not bool(row.get("target_match"))
            )
        ]
        return {
            "ok": not missing and not failed,
            "required": list(PRODUCTION_REQUIRED_REPORT_TYPES),
            "missing": missing,
            "failed": failed,
            "reports": reports,
        }

    def production_requirements(self):
        last_exc = None
        for _ in range(5):
            conn = self.get_control_db()
            try:
                return self._production_requirements_on_conn(conn)
            except Exception as exc:
                last_exc = exc
                if not self._is_sqlite_locked_error(exc):
                    raise
                time.sleep(0.15)
            finally:
                conn.close()
        if last_exc is not None:
            raise last_exc
        return {"ok": False, "required": list(PRODUCTION_REQUIRED_REPORT_TYPES), "missing": list(PRODUCTION_REQUIRED_REPORT_TYPES), "failed": [], "reports": {}}

    def upload_production_report(
        self,
        *,
        actor,
        report_type,
        report_hash,
        target_commit="",
        target_branch="",
        server_mode="",
        test_result="",
        passed=False,
        critical_findings_count=0,
        high_findings_count=0,
        unresolved_findings=None,
        tester="",
        signature="",
        raw_report=None,
        key_version="",
        report_source="manual_signed_upload",
    ):
        report_type = str(report_type or "").strip()
        if report_type not in PRODUCTION_REQUIRED_REPORT_TYPES:
            return {"ok": False, "msg": "report_type 不在 production gate 清單"}
        target_commit = str(target_commit or "").strip()
        target_branch = str(target_branch or "").strip()
        server_mode = str(server_mode or "").strip()
        test_result = str(test_result or "").strip().lower()
        tester = str(tester or self._actor_name(actor) or "").strip()
        signature = str(signature or "").strip()
        if not target_commit or not target_branch or not server_mode or not test_result or not tester or not signature:
            return {"ok": False, "msg": "production report 缺少 target_commit/target_branch/server_mode/test_result/tester/signature"}
        if test_result not in {"pass", "passed"} or not passed:
            return {"ok": False, "msg": "production report 必須明確 pass"}
        if int(critical_findings_count or 0) != 0 or int(high_findings_count or 0) != 0:
            return {"ok": False, "msg": "production report 不允許 critical/high finding"}
        if unresolved_findings:
            return {"ok": False, "msg": "production report 不允許 unresolved finding"}
        attestation = self._prepare_production_report_attestation(
            report_type=report_type,
            raw_report=raw_report,
            target_commit=target_commit,
            target_branch=target_branch,
            server_mode=server_mode,
            test_result=test_result,
            passed=passed,
            critical_findings_count=critical_findings_count,
            high_findings_count=high_findings_count,
            unresolved_findings=unresolved_findings,
            tester=tester,
            report_source=report_source,
        )
        if not attestation.get("ok"):
            return {"ok": False, "msg": "production report 需要 raw_report，伺服器必須重算 hash 並驗證簽章", "reason": attestation.get("reason") or "missing_raw_report"}
        report_hash = str(report_hash or "").strip()
        if not SHA256_REPORT_HASH_RE.fullmatch(report_hash):
            return {"ok": False, "msg": "report_hash 必須是 sha256:<64 hex>"}
        if report_hash != attestation["report_hash"]:
            return {"ok": False, "msg": "report_hash 與 raw_report 內容不一致", "expected_report_hash": attestation["report_hash"]}
        provided_key_version = str(key_version or "").strip()
        if provided_key_version and provided_key_version != attestation["key_version"]:
            return {"ok": False, "msg": "key_version 與伺服器可驗證金鑰不一致", "expected_key_version": attestation["key_version"]}
        if signature != attestation["signature"]:
            return {"ok": False, "msg": "signature 驗證失敗，請確認使用伺服器可驗證的正式報告簽章", "expected_key_version": attestation["key_version"]}
        report_id = f"prodrep_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(4)}"
        conn = self.get_control_db()
        try:
            self.ensure_schema(conn)
            self._record_security_key_on_conn(conn, purpose="server_mode_report", key_version=attestation["key_version"], status="active")
            replay = conn.execute(
                """
                SELECT id FROM production_entry_reports
                WHERE report_type=? AND report_hash=? AND target_commit=?
                LIMIT 1
                """,
                (report_type, report_hash, target_commit),
            ).fetchone()
            if replay:
                return {"ok": False, "msg": "production report 重複提交", "existing_report_id": replay["id"]}
            conn.execute(
                """
                INSERT INTO production_entry_reports
                (id, report_type, report_hash, target_commit, target_branch, server_mode, test_result,
                 pass, critical_findings_count, high_findings_count, unresolved_findings_json, tester, signature,
                 raw_report_json, report_source, trust_level, key_version, verified_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    report_type,
                    report_hash,
                    target_commit,
                    target_branch,
                    server_mode,
                    test_result,
                    1 if passed else 0,
                    int(critical_findings_count or 0),
                    int(high_findings_count or 0),
                    json.dumps(unresolved_findings or [], ensure_ascii=False, sort_keys=True),
                    tester,
                    signature,
                    attestation["raw_report_json"],
                    str(report_source or "manual_signed_upload").strip() or "manual_signed_upload",
                    "verified",
                    attestation["key_version"],
                    datetime.now().isoformat(),
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
            requirements = self._production_requirements_on_conn(conn)
            return {
                "ok": True,
                "report_id": report_id,
                "trust_level": "verified",
                "signature_valid": True,
                "key_version": attestation["key_version"],
                "requirements": requirements,
            }
        finally:
            conn.close()

    def enter_incident_lockdown(self, *, actor, trigger_type, reason, verification=None):
        conn = self.get_control_db()
        try:
            self.ensure_schema(conn)
            incident_id = self._enter_incident_lockdown_on_conn(
                conn,
                actor=actor,
                trigger_type=trigger_type,
                reason=reason,
                verification=verification or {},
            )
            conn.commit()
            self.audit("SERVER_MODE_INCIDENT_LOCKDOWN_ENTER", "-", user=self._actor_name(actor), success=True, detail=f"incident_id={incident_id},trigger={trigger_type},reason={reason}")
            return {"ok": True, "incident_id": incident_id, "mode": self.get_current_mode()}
        finally:
            conn.close()

    def incident_status(self):
        conn = self.get_control_db()
        try:
            self.ensure_schema(conn)
            row = conn.execute("SELECT * FROM incident_reports WHERE status='open' ORDER BY entered_at DESC LIMIT 1").fetchone()
            mode_row = conn.execute("SELECT * FROM server_modes WHERE id=1").fetchone()
            return {"ok": True, "incident": dict(row) if row else None, "mode": dict(mode_row) if mode_row else None}
        finally:
            conn.close()

    def resolve_incident(self, *, actor, confirm, notes="", verification=None):
        if confirm != "RESOLVE_INCIDENT":
            return {"ok": False, "msg": "confirm 必須等於 RESOLVE_INCIDENT"}
        conn = self.get_control_db()
        try:
            self.ensure_schema(conn)
            row = conn.execute("SELECT * FROM incident_reports WHERE status='open' ORDER BY entered_at DESC LIMIT 1").fetchone()
            if not row:
                return {"ok": False, "msg": "目前沒有 open incident"}
            now = datetime.now().isoformat()
            conn.execute(
                """
                UPDATE incident_reports
                SET status='resolved', resolved_by=?, resolved_at=?, resolution_notes=?, verification_json=?
                WHERE id=?
                """,
                (
                    self._actor_id(actor),
                    now,
                    notes or "",
                    json.dumps(verification or {}, ensure_ascii=False, sort_keys=True),
                    row["id"],
                ),
            )
            conn.commit()
            return {"ok": True, "incident_id": row["id"], "resolved_at": now}
        finally:
            conn.close()

    def _apply_production_upload_policy(self, conn):
        try:
            from services.security.upload_security import ensure_upload_security_schema, update_cloud_drive_security_policy
        except Exception:
            return {"ok": False, "msg": "上傳安全政策目前無法使用"}
        ensure_upload_security_schema(conn)
        policy, msg = update_cloud_drive_security_policy(conn, {
            "require_scan_before_download": True,
            "block_unclean_downloads": True,
            "warn_high_risk_downloads": True,
            "allow_inline_preview_for_high_risk": False,
            "e2ee_server_scan_claim_allowed": False,
            "revoke_shares_on_suspension": True,
            "scanner_enabled": True,
            "scanner_backend": "clamav",
            "scanner_timeout_seconds": 60,
            "fail_closed_on_scanner_error": True,
            "quarantine_on_infected": True,
            "validate_magic_mime": True,
            "deep_archive_scan_enabled": True,
            "max_archive_depth": 2,
            "office_macro_scan_enabled": True,
            "image_reencode_enabled": True,
            "image_reencode_max_pixels": 25_000_000,
            "yara_enabled": True,
            "max_archive_files": 200,
            "max_archive_uncompressed_bytes": 50 * 1024 * 1024,
            "max_daily_downloads": 500,
            "notes": "production mode: strict scan, fail-closed download, quarantine and content validation enabled",
        })
        if msg:
            return {"ok": False, "msg": msg}
        return {"ok": True, "policy": policy}

    def _apply_production_account_policy(self, conn, *, actor):
        user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "username" not in user_cols or "id" not in user_cols:
            return {"default_password_reset_required": 0, "test_accounts_disabled": 0, "sessions_revoked": 0}
        now = datetime.now().isoformat()

        default_where = ["username IN ({})".format(",".join("?" for _ in DEFAULT_ACCOUNT_NAMES))]
        default_params = list(DEFAULT_ACCOUNT_NAMES)
        if "is_default_password" in user_cols:
            default_where.append("COALESCE(is_default_password, 0)=1")
        default_rows = conn.execute(
            f"SELECT id FROM users WHERE {' OR '.join(default_where)}",
            tuple(default_params),
        ).fetchall()
        default_ids = [int(row["id"]) for row in default_rows]
        default_updates = []
        if "must_change_password" in user_cols:
            default_updates.append("must_change_password=1")
        if "is_default_password" in user_cols:
            default_updates.append("is_default_password=1")
        if "updated_at" in user_cols:
            default_updates.append("updated_at=?")
        if default_ids and default_updates:
            params = []
            if "updated_at" in user_cols:
                params.append(now)
            placeholders = ",".join("?" for _ in default_ids)
            conn.execute(
                f"UPDATE users SET {', '.join(default_updates)} WHERE id IN ({placeholders})",
                tuple(params + default_ids),
            )

        test_rows = conn.execute(
            "SELECT id FROM users WHERE username IN ({})".format(",".join("?" for _ in TEST_ACCOUNT_NAMES)),
            tuple(TEST_ACCOUNT_NAMES),
        ).fetchall()
        test_ids = [int(row["id"]) for row in test_rows]
        if test_ids and "status" in user_cols:
            updates = ["status='inactive'"]
            if "updated_at" in user_cols:
                updates.append("updated_at=?")
            params = [now] if "updated_at" in user_cols else []
            placeholders = ",".join("?" for _ in test_ids)
            conn.execute(
                f"UPDATE users SET {', '.join(updates)} WHERE id IN ({placeholders})",
                tuple(params + test_ids),
            )

        sessions_revoked = 0
        # When auth lives in the same SQLite db as the main `conn` (the
        # default in tests + small deployments), reuse the same connection.
        # Opening a second writer to the same db while we hold an open
        # transaction here serialises through SQLite's BEGIN-IMMEDIATE busy
        # timeout, which can wedge under load. Reuse `conn` when auth and
        # main db share a get_db; only open a fresh connection when the
        # auth db is genuinely separate (production split-db deployments).
        if self.get_auth_db is self.get_db:
            auth_conn = conn
            owned_auth_conn = False
        else:
            auth_conn = self.get_auth_db()
            owned_auth_conn = True
        try:
            session_cols = set()
            try:
                session_cols = {row["name"] for row in auth_conn.execute("PRAGMA table_info(sessions)").fetchall()}
            except Exception:
                session_cols = set()
            if test_ids and {"user_id", "is_revoked"}.issubset(session_cols):
                placeholders = ",".join("?" for _ in test_ids)
                updates = ["is_revoked=1"]
                params = []
                if "revoked_at" in session_cols:
                    updates.append("revoked_at=?")
                    params.append(now)
                cur = auth_conn.execute(
                    f"UPDATE sessions SET {', '.join(updates)} WHERE user_id IN ({placeholders}) AND COALESCE(is_revoked, 0)=0",
                    tuple(params + test_ids),
                )
                sessions_revoked = int(cur.rowcount or 0)
                if owned_auth_conn:
                    auth_conn.commit()
        finally:
            if owned_auth_conn:
                auth_conn.close()

        return {
            "default_password_reset_required": len(default_ids),
            "test_accounts_disabled": len(test_ids),
            "sessions_revoked": sessions_revoked,
            "password_policy": "forced reset uses the account password-strength policy",
            "actor": actor.get("username") if hasattr(actor, "get") else None,
        }

    def _apply_production_hardening(self, *, actor):
        conn = self.get_db()
        try:
            ensure_snapshot_schema(conn)
            account_result = self._apply_production_account_policy(conn, actor=actor)
            upload_policy = self._apply_production_upload_policy(conn)
            if not upload_policy.get("ok"):
                conn.rollback()
                return {"ok": False, "msg": upload_policy.get("msg") or "production upload policy failed"}
            conn.commit()
            return {"ok": True, "accounts": account_result, "cloud_drive_policy": upload_policy.get("policy")}
        finally:
            conn.close()

    def _apply_internal_test_hardening(self):
        conn = self.get_db()
        try:
            ensure_snapshot_schema(conn)
            now = datetime.now().isoformat()
            user_ids = [
                int(row["id"])
                for row in conn.execute(
                    "SELECT id FROM users WHERE username<>'root'"
                ).fetchall()
            ]
            # Reuse `conn` when auth shares the main db (see same-db reasoning
            # in _apply_production_account_policy above).
            if self.get_auth_db is self.get_db:
                auth_conn = conn
                owned_auth_conn = False
            else:
                auth_conn = self.get_auth_db()
                owned_auth_conn = True
            try:
                session_cols = set()
                try:
                    session_cols = {row["name"] for row in auth_conn.execute("PRAGMA table_info(sessions)").fetchall()}
                except Exception:
                    session_cols = set()
                revoked = 0
                if user_ids and {"user_id", "is_revoked"}.issubset(session_cols):
                    updates = ["is_revoked=1"]
                    params = []
                    if "revoked_at" in session_cols:
                        updates.append("revoked_at=?")
                        params.append(now)
                    placeholders = ",".join("?" for _ in user_ids)
                    cur = auth_conn.execute(
                        f"""
                        UPDATE sessions
                        SET {', '.join(updates)}
                        WHERE COALESCE(is_revoked, 0)=0
                              AND user_id IN ({placeholders})
                        """,
                        tuple(params + user_ids),
                    )
                    revoked = int(cur.rowcount or 0)
                    if owned_auth_conn:
                        auth_conn.commit()
            finally:
                if owned_auth_conn:
                    auth_conn.close()
            conn.commit()
            return {"ok": True, "sessions_revoked": revoked}
        finally:
            conn.close()

    def switch_mode(self, *, target_mode, actor, confirm, notes=None):
        original_target = str(target_mode or "").strip().lower()
        target_mode = self._normalize_mode(original_target)
        profile = self.get_profile(target_mode)
        if not profile:
            return {"ok": False, "msg": "server mode 錯誤"}
        expected_confirm = MODE_CONFIRM_PHRASES.get(target_mode, "SWITCH_CUSTOM_MODE")
        if confirm != expected_confirm:
            return {"ok": False, "msg": f"confirm 必須等於 {expected_confirm}"}
        if target_mode == "production":
            requirements = self.production_requirements()
            if not requirements.get("ok"):
                return {
                    "ok": False,
                    "msg": "production gate 未通過，缺少報告或仍有 critical/high finding",
                    "requirements": requirements,
                }
            if self.integrity_guard:
                try:
                    allowed, high_risk_count = self.integrity_guard.can_enter_preprod()
                except Exception:
                    allowed, high_risk_count = False, 1
                if not allowed:
                    conn = self.get_control_db()
                    try:
                        self.ensure_schema(conn)
                        current_row = conn.execute("SELECT current_mode FROM server_modes WHERE id=1").fetchone()
                        self._record_mode_switch(
                            conn,
                            from_mode=(current_row["current_mode"] if current_row else "test"),
                            to_mode="production",
                            actor=actor,
                            reason=notes or "",
                            success=False,
                            error_message="integrity guard high risk finding",
                            config_diff={"high_risk_count": high_risk_count},
                        )
                        self._enter_incident_lockdown_on_conn(
                            conn,
                            actor=actor,
                            trigger_type="integrity_high_risk",
                            reason="production entry blocked by high risk Integrity Guard finding",
                            verification={"high_risk_count": high_risk_count},
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    return {
                        "ok": False,
                        "msg": "Integrity Guard 存在高風險異常，不允許進入 production，已進入 incident_lockdown",
                        "high_risk_count": high_risk_count,
                        "incident_lockdown": True,
                    }
        applied_settings = {}
        production_result = None
        internal_test_result = None
        control_conn = self.get_control_db()
        main_conn = self.get_db()
        try:
            self.ensure_schema(control_conn)
            current_row = control_conn.execute("SELECT current_mode FROM server_modes WHERE id=1").fetchone()
            current = self._normalize_mode(current_row["current_mode"] if current_row else "test")
            if current == "incident_lockdown" and target_mode == "superweak":
                self._record_mode_switch(
                    control_conn,
                    from_mode=current,
                    to_mode=target_mode,
                    actor=actor,
                    reason=notes or "",
                    success=False,
                    error_message="incident_lockdown 不允許切換到 superweak",
                )
                control_conn.commit()
                return {"ok": False, "msg": "incident_lockdown 不允許切換到 superweak"}
            current_settings = self._settings_snapshot(main_conn)
            config_diff = self._config_diff(current_settings, {**(profile.get("settings") or {}), **(profile.get("thresholds") or {})})
        finally:
            control_conn.close()
            main_conn.close()

        checkpoint = self.create_mode_checkpoint(
            actor=actor,
            target_mode=target_mode,
            reason=notes or "",
            snapshot_type="before_superweak" if target_mode == "superweak" else "mode_checkpoint",
            from_mode=current,
        )
        if not checkpoint.get("ok"):
            conn = self.get_control_db()
            try:
                self.ensure_schema(conn)
                self._record_mode_switch(
                    conn,
                    from_mode=current,
                    to_mode=target_mode,
                    actor=actor,
                    reason=notes or "",
                    success=False,
                    error_message=checkpoint.get("msg") or checkpoint.get("error") or "checkpoint failed",
                )
                self._enter_incident_lockdown_on_conn(
                    conn,
                    actor=actor,
                    trigger_type="mode_switch_failed",
                    reason=f"checkpoint before {target_mode} failed",
                    verification=checkpoint,
                )
                conn.commit()
            finally:
                conn.close()
            return {**checkpoint, "msg": checkpoint.get("msg") or "checkpoint 建立失敗，已進入 incident_lockdown", "incident_lockdown": True}

        try:
            if self.save_settings:
                updates = {}
                updates.update(profile.get("settings") or {})
                updates.update(profile.get("thresholds") or {})
                applied_settings = self.save_settings(updates) if updates else {}
            if target_mode == "production":
                production_result = self._apply_production_hardening(actor=actor)
                if not production_result.get("ok"):
                    raise RuntimeError(production_result.get("msg") or "production hardening failed")
            if target_mode == "internal_test":
                internal_test_result = self._apply_internal_test_hardening()
        except Exception as exc:
            conn = self.get_control_db()
            try:
                self.ensure_schema(conn)
                self._record_mode_switch(
                    conn,
                    from_mode=current,
                    to_mode=target_mode,
                    actor=actor,
                    reason=notes or "",
                    checkpoint_id=checkpoint.get("checkpoint_id"),
                    snapshot_id=checkpoint.get("snapshot_id"),
                    success=False,
                    error_message=str(exc),
                    config_diff=config_diff,
                )
                self._enter_incident_lockdown_on_conn(
                    conn,
                    actor=actor,
                    trigger_type="mode_switch_failed",
                    reason=f"mode switch to {target_mode} failed: {exc}",
                    verification={"checkpoint": checkpoint, "target_mode": target_mode},
                )
                conn.commit()
            finally:
                conn.close()
            return {"ok": False, "msg": "模式切換套用設定失敗，已進入 incident_lockdown", "error": str(exc), "checkpoint": checkpoint}

        conn = self.get_control_db()
        try:
            self.ensure_schema(conn)
            now = datetime.now().isoformat()
            conn.execute(
                """
                UPDATE server_modes
                SET previous_mode=?, current_mode=?, active_snapshot_id=?, checkpoint_id=?,
                    mode_changed_by=?, mode_changed_at=?, notes=?, reason=?, config_json=?
                WHERE id=1
                """,
                (
                    current,
                    target_mode,
                    checkpoint.get("snapshot_id") if target_mode == "superweak" else None,
                    checkpoint.get("checkpoint_id"),
                    self._actor_id(actor),
                    now,
                    notes or "",
                    notes or "",
                    json.dumps(profile, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._record_mode_switch(
                conn,
                from_mode=current,
                to_mode=target_mode,
                actor=actor,
                reason=notes or "",
                checkpoint_id=checkpoint.get("checkpoint_id"),
                snapshot_id=checkpoint.get("snapshot_id"),
                success=True,
                config_diff=config_diff,
                restore_result={},
            )
            chain = verify_mode_switch_log_hash_chain(conn)
            if not chain.get("ok"):
                self._enter_incident_lockdown_on_conn(
                    conn,
                    actor=actor,
                    trigger_type="mode_switch_log_chain_broken",
                    reason=f"mode switch log hash chain failed after switching to {target_mode}",
                    verification=chain,
                )
                conn.commit()
                return {"ok": False, "msg": "mode switch log chain broken; incident_lockdown entered", "chain": chain, "incident_lockdown": True}
            conn.commit()
            self._mirror_current_mode_to_main_db(
                current_mode=target_mode,
                previous_mode=current,
                checkpoint_id=checkpoint.get("checkpoint_id"),
                snapshot_id=checkpoint.get("snapshot_id") if target_mode == "superweak" else None,
                actor_id=self._actor_id(actor),
                notes=notes or "",
                reason=notes or "",
                config_json=json.dumps(profile, ensure_ascii=False, sort_keys=True),
            )
            event = "SUPERWEAK_ENTER" if target_mode == "superweak" else "SERVER_MODE_CHANGE"
            self.audit(event, "-", user=self._actor_name(actor), success=True, detail=f"old_value={current},new_value={target_mode},profile={profile['name']},checkpoint={checkpoint.get('checkpoint_id')},snapshot={checkpoint.get('snapshot_id')},settings={applied_settings},production={production_result or {}},internal_test={internal_test_result or {}},reason={notes or ''}")
            return {"ok": True, "mode": self.get_current_mode(), "profile": profile, "applied_settings": applied_settings, "production": production_result, "internal_test": internal_test_result, "checkpoint": checkpoint}
        finally:
            conn.close()

    def enter_superweak(self, *, actor, confirm, notes=None):
        current = self.get_current_mode()
        if self._normalize_mode(current.get("current_mode")) == "superweak":
            return {"ok": False, "msg": "目前已是 superweak 模式"}
        return self.switch_mode(target_mode="superweak", actor=actor, confirm=confirm, notes=notes)

    def exit_superweak(self, *, actor, action, confirm, reason):
        current = self.get_current_mode()
        if self._normalize_mode(current.get("current_mode")) != "superweak":
            return {"ok": False, "msg": "目前不是 superweak 模式"}
        if action == "keep_dirty_state":
            return {"ok": False, "msg": "Server Mode v2 禁止保留 superweak dirty state；離開 superweak 必須還原 checkpoint"}
        if action == "restore":
            if confirm != "RESTORE_BEFORE_SUPERWEAK":
                return {"ok": False, "msg": "confirm 必須等於 RESTORE_BEFORE_SUPERWEAK"}
            snapshot_id = current["active_snapshot_id"]
            checkpoint_id = current.get("checkpoint_id")
            expected_checkpoint = None
            if checkpoint_id:
                conn = self.get_control_db()
                try:
                    self.ensure_schema(conn)
                    expected_checkpoint = self._checkpoint_record(conn, checkpoint_id)
                finally:
                    conn.close()
            result = self.snapshot_service.restore_snapshot(snapshot_id=snapshot_id, actor=actor, reason=reason or "exit superweak", dry_run=False)
            if not result.get("ok"):
                conn = self.get_control_db()
                try:
                    self.ensure_schema(conn)
                    self._record_mode_switch(
                        conn,
                        from_mode="superweak",
                        to_mode=current["previous_mode"] or "test",
                        actor=actor,
                        reason=reason or "",
                        checkpoint_id=checkpoint_id,
                        snapshot_id=snapshot_id,
                        success=False,
                        error_message=result.get("msg") or result.get("error") or "restore failed",
                        restore_result=result,
                    )
                    self._enter_incident_lockdown_on_conn(
                        conn,
                        actor=actor,
                        trigger_type="restore_validation_failed",
                        reason="exit superweak restore failed",
                        verification=result,
                    )
                    conn.commit()
                finally:
                    conn.close()
                return {**result, "incident_lockdown": True}
            validation = self.validate_checkpoint_restore(checkpoint_id=checkpoint_id, expected_checkpoint=expected_checkpoint) if checkpoint_id else {"ok": False, "msg": "missing checkpoint_id"}
            if not validation.get("ok"):
                conn = self.get_control_db()
                try:
                    self.ensure_schema(conn)
                    self._record_mode_switch(
                        conn,
                        from_mode="superweak",
                        to_mode=current["previous_mode"] or "test",
                        actor=actor,
                        reason=reason or "",
                        checkpoint_id=checkpoint_id,
                        snapshot_id=snapshot_id,
                        success=False,
                        error_message="checkpoint restore validation failed",
                        restore_result=validation,
                    )
                    self._enter_incident_lockdown_on_conn(
                        conn,
                        actor=actor,
                        trigger_type="restore_validation_failed",
                        reason="superweak checkpoint restore validation failed",
                        verification=validation,
                    )
                    conn.commit()
                finally:
                    conn.close()
                return {"ok": False, "msg": "superweak 還原驗證失敗，已進入 incident_lockdown", "restore": result, "validation": validation, "incident_lockdown": True}
            previous = self._normalize_mode(current["previous_mode"] or "test")
            conn = self.get_control_db()
            try:
                self.ensure_schema(conn)
                conn.execute(
                    """
                    UPDATE server_modes
                    SET current_mode=?, previous_mode='superweak', active_snapshot_id=NULL, checkpoint_id=NULL,
                        mode_changed_by=?, mode_changed_at=?, notes=?, reason=?
                    WHERE id=1
                    """,
                    (previous, self._actor_id(actor), datetime.now().isoformat(), reason or "", reason or ""),
                )
                self._record_mode_switch(
                    conn,
                    from_mode="superweak",
                    to_mode=previous,
                    actor=actor,
                    reason=reason or "",
                    checkpoint_id=checkpoint_id,
                    snapshot_id=snapshot_id,
                    success=True,
                    restore_result={"restore": result, "validation": validation},
                )
                conn.commit()
            finally:
                conn.close()
            self.audit("SUPERWEAK_EXIT_RESTORE", "-", user=self._actor_name(actor), success=True, detail=f"restored_snapshot={snapshot_id},checkpoint={checkpoint_id},new_value={previous},reason={reason}")
            return {"ok": True, "mode": self.get_current_mode(), "validation": validation, **result}
        return {"ok": False, "msg": "action 錯誤"}

    def recover_superweak_on_startup(self, *, actor=None):
        actor = actor or {"id": 0, "username": "system-startup", "role": "system"}
        current = self.get_current_mode()
        if self._normalize_mode(current.get("current_mode")) != "superweak":
            return {"ok": True, "recovered": False, "mode": current}
        snapshot_id = current.get("active_snapshot_id")
        checkpoint_id = current.get("checkpoint_id")
        if not snapshot_id or not checkpoint_id:
            conn = self.get_control_db()
            try:
                self.ensure_schema(conn)
                self._record_mode_switch(
                    conn,
                    from_mode="superweak",
                    to_mode="incident_lockdown",
                    actor=actor,
                    reason="startup superweak recovery failed: missing checkpoint/snapshot",
                    success=False,
                    error_message="missing active_snapshot_id or checkpoint_id",
                )
                self._enter_incident_lockdown_on_conn(
                    conn,
                    actor=actor,
                    trigger_type="superweak_recovery_failed",
                    reason="startup found superweak without active checkpoint",
                    verification={"mode": current},
                )
                conn.commit()
            finally:
                conn.close()
            return {"ok": False, "recovered": False, "incident_lockdown": True, "msg": "Superweak 啟動恢復缺少 checkpoint"}
        expected_checkpoint = None
        conn = self.get_control_db()
        try:
            self.ensure_schema(conn)
            expected_checkpoint = self._checkpoint_record(conn, checkpoint_id)
        finally:
            conn.close()
        result = self.snapshot_service.restore_snapshot(
            snapshot_id=snapshot_id,
            actor=actor,
            reason="startup recovery after superweak crash",
            dry_run=False,
        )
        validation = self.validate_checkpoint_restore(checkpoint_id=checkpoint_id, expected_checkpoint=expected_checkpoint)
        previous = self._normalize_mode(current.get("previous_mode") or "test")
        conn = self.get_control_db()
        try:
            self.ensure_schema(conn)
            if result.get("ok") and validation.get("ok"):
                conn.execute(
                    """
                    UPDATE server_modes
                    SET current_mode=?, previous_mode='superweak', active_snapshot_id=NULL, checkpoint_id=NULL,
                        mode_changed_by=?, mode_changed_at=?, notes=?, reason=?
                    WHERE id=1
                    """,
                    (
                        previous,
                        self._actor_id(actor),
                        datetime.now().isoformat(),
                        "startup recovered superweak dirty state",
                        "startup recovered superweak dirty state",
                    ),
                )
                self._record_mode_switch(
                    conn,
                    from_mode="superweak",
                    to_mode=previous,
                    actor=actor,
                    reason="startup recovered superweak dirty state",
                    checkpoint_id=checkpoint_id,
                    snapshot_id=snapshot_id,
                    success=True,
                    restore_result={"restore": result, "validation": validation},
                )
                conn.commit()
                self.audit("SUPERWEAK_STARTUP_RECOVERY", "-", user=self._actor_name(actor), success=True, detail=f"snapshot={snapshot_id},checkpoint={checkpoint_id},new_value={previous}")
                return {"ok": True, "recovered": True, "mode": self.get_current_mode(), "restore": result, "validation": validation}
            self._record_mode_switch(
                conn,
                from_mode="superweak",
                to_mode="incident_lockdown",
                actor=actor,
                reason="startup superweak recovery validation failed",
                checkpoint_id=checkpoint_id,
                snapshot_id=snapshot_id,
                success=False,
                error_message="restore or validation failed",
                restore_result={"restore": result, "validation": validation},
            )
            self._enter_incident_lockdown_on_conn(
                conn,
                actor=actor,
                trigger_type="superweak_recovery_failed",
                reason="startup superweak restore validation failed",
                verification={"restore": result, "validation": validation},
            )
            conn.commit()
            return {"ok": False, "recovered": False, "incident_lockdown": True, "restore": result, "validation": validation}
        finally:
            conn.close()


from . import tester_shadow as _tester_shadow

for _name in ('create_tester_token', 'revoke_tester_token', 'list_tester_tokens', '_write_tester_token_audit', 'active_tester_token', 'tester_shadow_state', 'set_tester_shadow_role', 'adjust_tester_shadow_wallet'):
    setattr(ServerModeService, _name, getattr(_tester_shadow, _name))

del _tester_shadow
del _name
