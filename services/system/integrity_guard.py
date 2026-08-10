import hashlib
import hmac
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path


MANIFEST_VERSION = 1
MANIFEST_FILENAME = "integrity_manifest.json"
CONFIRM_APPROVE = "APPROVE INTEGRITY UPDATE"
AUTO_APPROVE_PENDING_AFTER = timedelta(days=1)
FINDING_STATUSES = {"pending", "approved", "rejected", "ignored"}
CHANGE_TYPES = {"modified", "added", "deleted"}
HIGH_RISK_CATEGORIES = {
    "auth",
    "admin_root",
    "integrity_guard",
    "migration",
    "snapshot",
    "storage_security",
    "server_entrypoint",
    "dependencies",
    "config",
    "maintenance",
}

EXCLUDED_DIRS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "anchors",
    "cache",
    "chats",
    "dist",
    "logs",
    "node_modules",
    "reports",
    "runtime",
    "snapshots",
    "storage",
    "uploads",
}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".log", ".tmp", ".swp"}
EXCLUDED_NAMES = {
    ".chain_seed",
    ".csrfkey",
    ".fkey",
    ".integrity_key",
    MANIFEST_FILENAME,
    "cert.pem",
    "key.pem",
}
ROOT_PROTECTED_FILES = {
    ".env.production.example",
    "README.md",
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "requirements.txt",
    "server.py",
    "vite.config.js",
    "vite.config.ts",
}
PROTECTED_DIRS = {"routes", "services", "public", "scripts", "database", "tests"}
PROTECTED_EXTENSIONS = {".py", ".js", ".css", ".html", ".sql", ".json", ".md", ".sh", ".toml", ".txt", ".yml", ".yaml"}


def ensure_integrity_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integrity_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            category TEXT,
            risk_level TEXT NOT NULL,
            change_type TEXT NOT NULL,
            old_hash TEXT,
            new_hash TEXT,
            old_size INTEGER,
            new_size INTEGER,
            old_mtime TEXT,
            new_mtime TEXT,
            detected_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            reviewed_by TEXT,
            reviewed_at TEXT,
            review_note TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integrity_scan_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            files_checked INTEGER NOT NULL DEFAULT 0,
            findings_created INTEGER NOT NULL DEFAULT 0,
            high_risk_count INTEGER NOT NULL DEFAULT 0,
            manifest_valid INTEGER NOT NULL DEFAULT 0,
            manifest_signature_valid INTEGER NOT NULL DEFAULT 0,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS integrity_manifest_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version INTEGER NOT NULL,
            manifest_hash TEXT NOT NULL,
            manifest_signature TEXT NOT NULL,
            approved_by TEXT NOT NULL,
            approved_at TEXT NOT NULL,
            note TEXT
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_integrity_findings_status ON integrity_findings(status, risk_level, detected_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_integrity_findings_path ON integrity_findings(file_path, status)")


def _now():
    return datetime.now().isoformat()


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value or ""))
    except Exception:
        return None


def _canonical_json(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mtime_iso(path):
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat()
    except Exception:
        return ""


class IntegrityGuard:
    def __init__(self, *, base_dir, manifest_path=None, signing_key=b"", get_db=None, audit=None):
        self.base_dir = Path(base_dir).resolve()
        self.manifest_path = Path(manifest_path or (self.base_dir / MANIFEST_FILENAME)).resolve()
        key = signing_key or b""
        self.signing_key = key if isinstance(key, bytes) else str(key).encode("utf-8")
        self.get_db = get_db
        self.audit = audit or (lambda *args, **kwargs: None)

    def ensure_schema(self, conn):
        ensure_integrity_schema(conn)

    def _rel(self, path):
        return str(Path(path).resolve().relative_to(self.base_dir)).replace(os.sep, "/")

    def should_protect(self, rel_path):
        rel = str(rel_path).replace("\\", "/").strip("/")
        if not rel or rel in EXCLUDED_NAMES:
            return False
        parts = tuple(Path(rel).parts)
        if any(part in EXCLUDED_DIRS for part in parts):
            return False
        name = parts[-1] if parts else rel
        suffix = Path(name).suffix.lower()
        if suffix in EXCLUDED_SUFFIXES:
            return False
        if len(parts) == 1:
            return rel in ROOT_PROTECTED_FILES
        if parts[0] not in PROTECTED_DIRS:
            return False
        if parts[0] == "database":
            return suffix == ".sql"
        return suffix in PROTECTED_EXTENSIONS or parts[0] == "scripts"

    def category_for_path(self, rel_path):
        rel = str(rel_path).replace("\\", "/")
        low = rel.lower()
        if rel == "server.py":
            return "server_entrypoint"
        if "integrity_guard" in low or "integrity" in low and ("system_admin" in low or "50-admin" in low):
            return "integrity_guard"
        if low == ".env.production.example":
            return "config"
        if low in {"requirements.txt", "package.json", "package-lock.json", "pyproject.toml"} or "vite.config" in low:
            return "dependencies"
        if low == "bootstrap.schema.sql" or "bootstrap.py" in low:
            return "migration"
        if "auth" in low or "password" in low or "access_control" in low or low.startswith("routes/public.py"):
            return "auth"
        if "system_admin" in low or "admin" in low or "root" in low or "50-admin.js" in low:
            return "admin_root"
        if "snapshot" in low or "restore" in low or "superweak" in low:
            return "snapshot"
        if "maintenance" in low or "server_mode" in low:
            return "maintenance"
        if "upload" in low or "download" in low or "storage" in low or "files.py" in low:
            return "storage_security"
        if low.startswith("routes/") or low.startswith("services/"):
            return "backend"
        if low.startswith("public/"):
            return "frontend"
        if low.startswith("scripts/"):
            return "scripts"
        if low.endswith(".md"):
            return "docs"
        return "other"

    def risk_for_category(self, category):
        if category in HIGH_RISK_CATEGORIES:
            return "high"
        if category in {"backend", "frontend", "scripts"}:
            return "medium"
        return "low"

    def file_record(self, path):
        p = Path(path).resolve()
        rel = self._rel(p)
        stat = p.stat()
        category = self.category_for_path(rel)
        return {
            "file_path": rel,
            "sha256": _sha256_file(p),
            "size": int(stat.st_size),
            "mtime": _mtime_iso(p),
            "category": category,
        }

    def collect_files(self):
        records = {}
        for root, dirs, files in os.walk(self.base_dir):
            root_path = Path(root)
            rel_root = "" if root_path == self.base_dir else self._rel(root_path)
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".cache")]
            for name in files:
                path = root_path / name
                try:
                    rel = self._rel(path)
                except Exception:
                    continue
                if self.should_protect(rel):
                    records[rel] = self.file_record(path)
        return dict(sorted(records.items()))

    def count_protected_files(self):
        """Count the current protection scope without reading or hashing files."""
        count = 0
        for root, dirs, files in os.walk(self.base_dir):
            root_path = Path(root)
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS and not d.startswith(".cache")]
            for name in files:
                path = root_path / name
                try:
                    rel = self._rel(path)
                except Exception:
                    continue
                if self.should_protect(rel):
                    count += 1
        return count

    def _manifest_body(self, entries):
        return {
            "version": MANIFEST_VERSION,
            "generated_at": _now(),
            "files": [entries[key] for key in sorted(entries)],
        }

    def _sign_body(self, body):
        material = _canonical_json(body).encode("utf-8")
        signature = hmac.new(self.signing_key, material, hashlib.sha256).hexdigest()
        return _sha256_bytes(material), signature

    def write_manifest(self, entries, *, approved_by, note=""):
        body = self._manifest_body(entries)
        manifest_hash, signature = self._sign_body(body)
        payload = {**body, "manifest_hash": manifest_hash, "manifest_signature": signature}
        tmp = self.manifest_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.manifest_path)
        if self.get_db:
            conn = self.get_db()
            try:
                self.ensure_schema(conn)
                conn.execute(
                    "INSERT INTO integrity_manifest_versions "
                    "(version, manifest_hash, manifest_signature, approved_by, approved_at, note) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (MANIFEST_VERSION, manifest_hash, signature, approved_by, _now(), (note or "")[:1000]),
                )
                conn.commit()
            finally:
                conn.close()
        return payload

    def load_manifest(self):
        if not self.manifest_path.exists():
            return None, "missing"
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None, "invalid_json"
        signature = str(payload.get("manifest_signature") or "")
        manifest_hash = str(payload.get("manifest_hash") or "")
        body = {key: payload.get(key) for key in ("version", "generated_at", "files")}
        expected_hash, expected_sig = self._sign_body(body)
        if manifest_hash != expected_hash or not hmac.compare_digest(signature, expected_sig):
            return payload, "bad_signature"
        files = payload.get("files")
        if not isinstance(files, list):
            return payload, "invalid_files"
        return payload, "ok"

    def manifest_entries(self, payload):
        entries = {}
        for item in payload.get("files") or []:
            if isinstance(item, dict) and item.get("file_path"):
                entries[str(item["file_path"])] = dict(item)
        return entries

    def _has_manifest_versions(self, conn):
        row = conn.execute("SELECT COUNT(*) AS c FROM integrity_manifest_versions").fetchone()
        return int(row["c"] if hasattr(row, "keys") else row[0]) > 0

    def _finding_exists(self, conn, file_path, change_type, old_hash, new_hash):
        row = conn.execute(
            "SELECT id FROM integrity_findings WHERE file_path=? AND change_type=? AND status='pending' "
            "AND COALESCE(old_hash, '')=COALESCE(?, '') AND COALESCE(new_hash, '')=COALESCE(?, '') LIMIT 1",
            (file_path, change_type, old_hash, new_hash),
        ).fetchone()
        return bool(row)

    def _is_clean_git_checkout(self):
        git_dir = self.base_dir / ".git"
        if not git_dir.exists():
            return False
        try:
            result = subprocess.run(
                ["git", "-C", str(self.base_dir), "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception:
            return False
        return result.returncode == 0 and not str(result.stdout or "").strip()

    def _health_state(self, conn, *, summary, last_scan):
        health = {"level": "ok", "detail": ""}
        if int(summary.get("high_risk_pending") or 0) <= 0:
            return health
        rows = conn.execute(
            "SELECT file_path, category FROM integrity_findings WHERE status='pending' AND risk_level='high'"
        ).fetchall()
        deploy_review_pending = bool(rows)
        for row in rows:
            data = dict(row)
            if data.get("file_path") == MANIFEST_FILENAME or data.get("category") == "integrity_guard":
                deploy_review_pending = False
                break
        if (
            deploy_review_pending
            and last_scan
            and str(last_scan.get("status") or "") == "findings"
            and bool(last_scan.get("manifest_valid"))
            and bool(last_scan.get("manifest_signature_valid"))
            and self._is_clean_git_checkout()
        ):
            # A clean post-deploy checkout still requires root review and
            # rebaseline, but it should not look identical to manifest
            # tampering or unsigned source drift.
            return {
                "level": "degraded",
                "detail": "偵測到已部署但尚未 rebaseline 的程式碼變更；production 仍應維持阻擋，請 root 檢查後刷新 integrity baseline",
            }
        return {"level": "critical", "detail": "高風險 integrity finding 待人工審核"}

    def create_finding(self, conn, *, file_path, category, risk_level, change_type, old_hash=None, new_hash=None, old_size=None, new_size=None, old_mtime=None, new_mtime=None):
        if change_type not in CHANGE_TYPES:
            change_type = "modified"
        if self._finding_exists(conn, file_path, change_type, old_hash, new_hash):
            return False
        conn.execute(
            "INSERT INTO integrity_findings "
            "(file_path, category, risk_level, change_type, old_hash, new_hash, old_size, new_size, old_mtime, new_mtime, detected_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            (
                file_path,
                category,
                risk_level,
                change_type,
                old_hash,
                new_hash,
                old_size,
                new_size,
                old_mtime,
                new_mtime,
                _now(),
            ),
        )
        return True

    def scan(self, *, actor="system", create_initial_manifest=True):
        started = _now()
        conn = self.get_db()
        findings_created = 0
        files_checked = 0
        high_risk_count = 0
        manifest_valid = False
        signature_valid = False
        status = "ok"
        error_message = ""
        try:
            self.ensure_schema(conn)
            run_cur = conn.execute(
                "INSERT INTO integrity_scan_runs "
                "(started_at, status, manifest_valid, manifest_signature_valid) VALUES (?, 'running', 0, 0)",
                (started,),
            )
            run_id = run_cur.lastrowid
            conn.commit()
            payload, manifest_state = self.load_manifest()
            if manifest_state == "missing":
                if create_initial_manifest and not self._has_manifest_versions(conn):
                    current = self.collect_files()
                    files_checked = len(current)
                    self.write_manifest(current, approved_by=str(actor or "system"), note="initial integrity baseline")
                    manifest_valid = True
                    signature_valid = True
                    status = "baseline_created"
                else:
                    created = self.create_finding(
                        conn,
                        file_path=MANIFEST_FILENAME,
                        category="integrity_guard",
                        risk_level="high",
                        change_type="deleted",
                        old_hash="manifest_expected",
                        new_hash=None,
                    )
                    findings_created += 1 if created else 0
                    high_risk_count += 1 if created else 0
                    status = "findings"
            elif manifest_state != "ok":
                manifest_hash = _sha256_file(self.manifest_path) if self.manifest_path.exists() else None
                created = self.create_finding(
                    conn,
                    file_path=MANIFEST_FILENAME,
                    category="integrity_guard",
                    risk_level="high",
                    change_type="modified",
                    old_hash=str((payload or {}).get("manifest_hash") or "valid_signature_required"),
                    new_hash=manifest_hash,
                )
                findings_created += 1 if created else 0
                high_risk_count += 1 if created else 0
                status = "findings"
                manifest_valid = False
                signature_valid = False
            else:
                manifest_valid = True
                signature_valid = True
                old_entries = self.manifest_entries(payload)
                current = self.collect_files()
                files_checked = len(current)
                for rel, old in old_entries.items():
                    category = old.get("category") or self.category_for_path(rel)
                    risk = self.risk_for_category(category)
                    if rel not in current:
                        created = self.create_finding(
                            conn,
                            file_path=rel,
                            category=category,
                            risk_level=risk,
                            change_type="deleted",
                            old_hash=old.get("sha256"),
                            new_hash=None,
                            old_size=old.get("size"),
                            new_size=None,
                            old_mtime=old.get("mtime"),
                            new_mtime=None,
                        )
                        findings_created += 1 if created else 0
                        high_risk_count += 1 if created and risk == "high" else 0
                    else:
                        new = current[rel]
                        if old.get("sha256") != new.get("sha256") or int(old.get("size") or 0) != int(new.get("size") or 0):
                            created = self.create_finding(
                                conn,
                                file_path=rel,
                                category=category,
                                risk_level=risk,
                                change_type="modified",
                                old_hash=old.get("sha256"),
                                new_hash=new.get("sha256"),
                                old_size=old.get("size"),
                                new_size=new.get("size"),
                                old_mtime=old.get("mtime"),
                                new_mtime=new.get("mtime"),
                            )
                            findings_created += 1 if created else 0
                            high_risk_count += 1 if created and risk == "high" else 0
                for rel, new in current.items():
                    if rel in old_entries:
                        continue
                    category = new.get("category") or self.category_for_path(rel)
                    risk = self.risk_for_category(category)
                    created = self.create_finding(
                        conn,
                        file_path=rel,
                        category=category,
                        risk_level=risk,
                        change_type="added",
                        old_hash=None,
                        new_hash=new.get("sha256"),
                        old_size=None,
                        new_size=new.get("size"),
                        old_mtime=None,
                        new_mtime=new.get("mtime"),
                    )
                    findings_created += 1 if created else 0
                    high_risk_count += 1 if created and risk == "high" else 0
                if findings_created:
                    status = "findings"

            conn.execute(
                "UPDATE integrity_scan_runs SET finished_at=?, status=?, files_checked=?, findings_created=?, "
                "high_risk_count=?, manifest_valid=?, manifest_signature_valid=?, error_message=? WHERE id=?",
                (_now(), status, files_checked, findings_created, high_risk_count, 1 if manifest_valid else 0, 1 if signature_valid else 0, error_message, run_id),
            )
            conn.commit()
            if findings_created:
                self.audit("INTEGRITY_FINDINGS_DETECTED", "-", user=str(actor or "system"), success=True, detail=f"findings_created={findings_created},high_risk_count={high_risk_count}")
            return self.status(conn=conn)
        except Exception as exc:
            error_message = str(exc)
            try:
                conn.execute(
                    "INSERT INTO integrity_scan_runs (started_at, finished_at, status, error_message) VALUES (?, ?, 'error', ?)",
                    (started, _now(), error_message[:1000]),
                )
                conn.commit()
            except Exception:
                pass
            return {"ok": False, "status": "error", "error_message": error_message}
        finally:
            conn.close()

    def status(self, conn=None):
        close = False
        if conn is None:
            conn = self.get_db()
            close = True
        try:
            self.ensure_schema(conn)
            auto_approved = self.auto_approve_expired_findings(conn=conn)
            run = conn.execute("SELECT * FROM integrity_scan_runs ORDER BY id DESC LIMIT 1").fetchone()
            counts = conn.execute(
                "SELECT status, risk_level, change_type, COUNT(*) AS c FROM integrity_findings GROUP BY status, risk_level, change_type"
            ).fetchall()
            # Status is queried by the health dashboard and must stay cheap.
            # Full content hashing belongs to scan(); counting the scope here
            # avoids re-reading the entire source tree on every health request.
            protected_count = self.count_protected_files() if self.base_dir.exists() else 0
            summary = {
                "pending": 0,
                "high_risk_pending": 0,
                "modified": 0,
                "added": 0,
                "deleted": 0,
            }
            for row in counts:
                data = dict(row)
                if data["status"] == "pending":
                    summary["pending"] += int(data["c"] or 0)
                    if data["risk_level"] == "high":
                        summary["high_risk_pending"] += int(data["c"] or 0)
                    if data["change_type"] in {"modified", "added", "deleted"}:
                        summary[data["change_type"]] += int(data["c"] or 0)
            health = self._health_state(conn, summary=summary, last_scan=dict(run) if run else None)
            return {
                "ok": True,
                "protected_files": protected_count,
                "summary": summary,
                "last_scan": dict(run) if run else None,
                "manifest_path": str(self.manifest_path),
                "preprod_allowed": summary["high_risk_pending"] == 0,
                "auto_approved_expired": auto_approved,
                "health": health,
                "deployment_review_pending": health["level"] == "degraded",
            }
        finally:
            if close:
                conn.close()

    def list_findings(self, *, status=None, limit=200):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self.auto_approve_expired_findings(conn=conn)
            params = []
            sql = "SELECT * FROM integrity_findings"
            if status:
                sql += " WHERE status=?"
                params.append(status)
            sql += " ORDER BY CASE risk_level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, detected_at DESC, id DESC LIMIT ?"
            params.append(max(1, min(int(limit or 200), 1000)))
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]
        finally:
            conn.close()

    def get_finding(self, finding_id):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            row = conn.execute("SELECT * FROM integrity_findings WHERE id=?", (int(finding_id),)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _current_entries_from_manifest(self):
        payload, state = self.load_manifest()
        if state != "ok":
            return {}, state
        return self.manifest_entries(payload), state

    def _apply_manifest_update_for_finding(self, finding):
        entries, state = self._current_entries_from_manifest()
        if state != "ok":
            entries = self.collect_files()
        file_path = finding["file_path"]
        change_type = finding["change_type"]
        if file_path == MANIFEST_FILENAME:
            return self.collect_files()
        if change_type == "deleted":
            entries.pop(file_path, None)
        else:
            target = (self.base_dir / file_path).resolve()
            if not target.exists() or not self.should_protect(file_path):
                raise ValueError("finding target no longer exists or is outside protected scope")
            entries[file_path] = self.file_record(target)
        return entries

    def rebaseline_paths(self, *, actor, file_paths, note=""):
        raw_paths = file_paths or []
        normalized = []
        seen = set()
        for path in raw_paths:
            rel = str(path or "").replace("\\", "/").strip("/")
            if not rel or rel == MANIFEST_FILENAME or rel in seen:
                continue
            seen.add(rel)
            normalized.append(rel)
        entries, state = self._current_entries_from_manifest()
        if state != "ok":
            entries = self.collect_files()
        updated_paths = []
        for rel in normalized:
            target = (self.base_dir / rel).resolve()
            if target.exists() and self.should_protect(rel):
                entries[rel] = self.file_record(target)
                updated_paths.append(rel)
            else:
                entries.pop(rel, None)
                updated_paths.append(rel)
        payload = self.write_manifest(
            entries,
            approved_by=str(actor or "system"),
            note=(note or "rebaseline selected integrity paths")[:1000],
        )
        approved_findings = 0
        if self.get_db:
            conn = self.get_db()
            try:
                self.ensure_schema(conn)
                reviewed_at = _now()
                review_note = (note or "approved by server update baseline refresh")[:1000]
                for rel in updated_paths:
                    cur = conn.execute(
                        "UPDATE integrity_findings SET status='approved', reviewed_by=?, reviewed_at=?, review_note=? "
                        "WHERE status='pending' AND file_path=?",
                        (str(actor or "system"), reviewed_at, review_note, rel),
                    )
                    approved_findings += int(cur.rowcount or 0)
                conn.commit()
                status = self.status(conn=conn)
            finally:
                conn.close()
        else:
            status = {"ok": True}
        self.audit(
            "INTEGRITY_BASELINE_REFRESHED",
            "-",
            user=str(actor or "system"),
            success=True,
            detail=f"updated_paths={len(updated_paths)},approved_findings={approved_findings}",
        )
        return {
            "ok": True,
            "updated_paths": updated_paths,
            "approved_findings": approved_findings,
            "manifest": payload,
            "status": status,
        }

    def auto_approve_expired_findings(self, *, actor="system:auto-approve", max_age=AUTO_APPROVE_PENDING_AFTER, conn=None):
        close = False
        if conn is None:
            conn = self.get_db()
            close = True
        approved = 0
        skipped = 0
        high_risk_skipped = 0
        try:
            self.ensure_schema(conn)
            cutoff = datetime.now() - max_age
            rows = conn.execute(
                "SELECT * FROM integrity_findings WHERE status='pending' ORDER BY detected_at ASC, id ASC"
            ).fetchall()
            for row in rows:
                finding = dict(row)
                detected_at = _parse_dt(finding.get("detected_at"))
                if not detected_at or detected_at > cutoff:
                    continue
                if finding.get("risk_level") == "high" or finding.get("category") in HIGH_RISK_CATEGORIES:
                    high_risk_skipped += 1
                    skipped += 1
                    note = "high-risk finding requires manual review; auto-approval skipped"
                    if finding.get("review_note") != note:
                        conn.execute(
                            "UPDATE integrity_findings SET review_note=? WHERE id=?",
                            (note, int(finding["id"])),
                        )
                        self.audit(
                            "INTEGRITY_FINDING_AUTO_APPROVE_SKIPPED_HIGH_RISK",
                            "-",
                            user=actor,
                            success=True,
                            detail=(
                                f"id={finding['id']},file_path={finding['file_path']},"
                                f"risk_level={finding['risk_level']},change_type={finding['change_type']}"
                            ),
                        )
                    continue
                try:
                    entries = self._apply_manifest_update_for_finding(finding)
                    self.write_manifest(entries, approved_by=actor, note=f"auto approve expired finding #{finding['id']}")
                    reviewed_at = _now()
                    conn.execute(
                        "UPDATE integrity_findings SET status='approved', reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                        (actor, reviewed_at, "auto-approved after 24 hours", int(finding["id"])),
                    )
                    approved += 1
                    self.audit(
                        "INTEGRITY_FINDING_AUTO_APPROVED",
                        "-",
                        user=actor,
                        success=True,
                        detail=(
                            f"id={finding['id']},file_path={finding['file_path']},"
                            f"risk_level={finding['risk_level']},change_type={finding['change_type']}"
                        ),
                    )
                except Exception as exc:
                    skipped += 1
                    self.audit(
                        "INTEGRITY_FINDING_AUTO_APPROVE_FAILED",
                        "-",
                        user=actor,
                        success=False,
                        detail=f"id={finding.get('id')},file_path={finding.get('file_path')},error={exc}",
                    )
            if approved or high_risk_skipped:
                conn.commit()
            return {"approved": approved, "skipped": skipped, "high_risk_skipped": high_risk_skipped}
        finally:
            if close:
                conn.close()

    def review_finding(self, finding_id, *, action, actor, note="", confirm=""):
        action = str(action or "").strip()
        if action not in {"approve", "reject", "ignore"}:
            return {"ok": False, "msg": "不支援的 integrity 操作"}
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            row = conn.execute("SELECT * FROM integrity_findings WHERE id=?", (int(finding_id),)).fetchone()
            if not row:
                return {"ok": False, "msg": "找不到 integrity finding"}
            finding = dict(row)
            if finding["status"] != "pending":
                return {"ok": False, "msg": "finding 已處理"}
            if action == "approve" and confirm != CONFIRM_APPROVE:
                return {"ok": False, "msg": f"confirm 必須等於 {CONFIRM_APPROVE}"}
            reviewed_at = _now()
            if action == "approve":
                try:
                    entries = self._apply_manifest_update_for_finding(finding)
                    self.write_manifest(entries, approved_by=actor["username"], note=note or f"approve finding #{finding_id}")
                except Exception as exc:
                    error_message = str(exc) or "integrity finding approve failed"
                    self.audit(
                        "INTEGRITY_FINDING_APPROVE_FAILED",
                        "-",
                        user=actor["username"],
                        success=False,
                        detail=f"id={finding_id},file_path={finding['file_path']},risk_level={finding['risk_level']},change_type={finding['change_type']},error={error_message}",
                    )
                    return {
                        "ok": False,
                        "msg": f"approve 失敗：{error_message}",
                        "error": "integrity_approve_failed",
                        "reason": error_message,
                        "finding": finding,
                    }
                new_status = "approved"
            elif action == "reject":
                new_status = "rejected"
            else:
                new_status = "ignored"
            conn.execute(
                "UPDATE integrity_findings SET status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                (new_status, actor["username"], reviewed_at, (note or "")[:1000], int(finding_id)),
            )
            conn.commit()
            self.audit(
                f"INTEGRITY_FINDING_{new_status.upper()}",
                "-",
                user=actor["username"],
                success=True,
                detail=f"id={finding_id},file_path={finding['file_path']},risk_level={finding['risk_level']},change_type={finding['change_type']}",
            )
            return {"ok": True, "msg": f"finding 已{new_status}", "finding": self.get_finding(finding_id), "status": self.status()}
        finally:
            conn.close()

    def can_enter_preprod(self):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self.auto_approve_expired_findings(conn=conn)
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM integrity_findings WHERE status IN ('pending', 'rejected') AND risk_level='high'"
            ).fetchone()
            count = int(row["c"] if hasattr(row, "keys") else row[0])
            return count == 0, count
        finally:
            conn.close()

    def export_report(self):
        return {
            "generated_at": _now(),
            "status": self.status(),
            "findings": self.list_findings(limit=1000),
        }
