"""Snapshot archive and restore service."""

import tempfile

from . import schema as _schema

globals().update(
    {
        name: value
        for name, value in _schema.__dict__.items()
        if not name.startswith("__")
    }
)


class SnapshotService:
    # Financial databases are still copied into snapshot packages for
    # forensics/export, but must never be replayed over the live append-only
    # ledger. Trading shares transactions with PointsChain in finance.db, so
    # restoring either side independently would also break settlement atomicity.
    PROTECTED_RESTORE_DATABASE_LABELS = frozenset({"finance", "points_chain", "trading"})

    def __init__(
        self,
        *,
        get_db,
        db_path,
        base_dir,
        runtime_base_dir=None,
        storage_root,
        audit,
        file_roots=None,
        config_files=None,
        runtime_secret_files=None,
        additional_db_paths=None,
        reset_points_chain=None,
        reset_audit_chain=None,
        post_restore_validators=None,
    ):
        self.get_db = get_db
        self.db_path = Path(db_path)
        self.base_dir = Path(base_dir)
        self.runtime_base_dir = Path(runtime_base_dir or base_dir)
        self.storage_root = Path(storage_root)
        self.snapshots_root = self.storage_root / "snapshots"
        self.imports_root = self.snapshots_root / ".imports"
        self.audit = audit
        self.reset_points_chain = reset_points_chain
        self.reset_audit_chain = reset_audit_chain
        self.post_restore_validators = list(post_restore_validators or [])
        self.file_roots = [Path(p) for p in (file_roots or []) if p]
        self.config_files = [Path(p) for p in (config_files or []) if p]
        self.runtime_secret_files = [Path(p) for p in (runtime_secret_files or []) if p]
        self.additional_db_paths = self._normalize_additional_db_paths(additional_db_paths)

    def set_post_restore_validators(self, validators):
        self.post_restore_validators = list(validators or [])

    def _run_post_restore_validators(self):
        results = []
        errors = []
        for name, validator in self.post_restore_validators:
            try:
                result = validator()
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            if not isinstance(result, dict):
                result = {"ok": bool(result), "result": result}
            item = {"name": name, **result}
            results.append(item)
            if item.get("ok") is not True:
                errors.append(item)
        return {"ok": not errors, "results": results, "errors": errors}

    def restore_in_progress(self):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            row = conn.execute(
                """
                SELECT id, snapshot_id, started_at
                FROM snapshot_restore_events
                WHERE status='restoring'
                ORDER BY started_at DESC
                LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def ensure_schema(self, conn):
        ensure_snapshot_schema(conn)

    def _snapshot_id(self):
        return f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"

    def _snapshot_dir(self, snapshot_id):
        if not _safe_snapshot_id(snapshot_id):
            raise ValueError("snapshot_id 格式錯誤")
        root = self.snapshots_root.resolve()
        path = (root / snapshot_id).resolve()
        if root not in path.parents:
            raise ValueError("snapshot path traversal blocked")
        return path

    def _portable_archive_path(self, snapshot_id):
        return self._snapshot_dir(snapshot_id) / f"{snapshot_id}.snapshot.tar.gz"

    def _stage_snapshot_dir_for_restore(self, snapshot_dir):
        snapshot_dir = Path(snapshot_dir).resolve(strict=False)
        file_roots = [Path(root).resolve(strict=False) for root in self.file_roots]
        if not any(root == snapshot_dir or root in snapshot_dir.parents for root in file_roots):
            return snapshot_dir, None
        stage_parent = Path(
            tempfile.mkdtemp(
                prefix="snapshot_restore_",
                dir=str(self.runtime_base_dir.resolve(strict=False)),
            )
        )
        staged_snapshot_dir = stage_parent / snapshot_dir.name
        # Restore must not delete its own archive when storage_root is one of the
        # managed runtime roots. Stage the full snapshot bundle outside file_roots
        # first, then clear runtime roots, and only then extract from the staged copy.
        shutil.copytree(snapshot_dir, staged_snapshot_dir)
        return staged_snapshot_dir, stage_parent

    def _local_snapshot_record(self, snapshot_id, *, actor_id=0, notes=None):
        snapshot_dir = self._snapshot_dir(snapshot_id)
        metadata_path = snapshot_dir / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        size_bytes = sum(p.stat().st_size for p in snapshot_dir.rglob("*") if p.is_file())
        snapshot_type = metadata.get("type") if metadata.get("type") in SNAPSHOT_TYPES else "manual"
        includes = metadata.get("includes") if isinstance(metadata.get("includes"), dict) else {"database": True, "uploads": True, "config": True}
        now = datetime.now().isoformat()
        return {
            "id": snapshot_id,
            "type": snapshot_type,
            "status": "ready",
            "created_by": int(actor_id or 0),
            "created_at": metadata.get("created_at") or now,
            "completed_at": now,
            "app_version": metadata.get("app_version") or "",
            "schema_version": str(metadata.get("schema_version") or ""),
            "source_mode": metadata.get("source_mode") or "imported",
            "includes_json": json.dumps(includes, ensure_ascii=False, sort_keys=True),
            "storage_path": str(snapshot_dir),
            "db_dump_path": str(snapshot_dir / "db.sqlite3.backup"),
            "files_archive_path": str(snapshot_dir / "uploads.tar.gz"),
            "config_archive_path": str(snapshot_dir / "config.tar.gz"),
            "checksum": metadata.get("checksum") or "",
            "size_bytes": size_bytes,
            "notes": notes if notes is not None else metadata.get("notes", ""),
        }

    def _upsert_local_snapshot_record(self, conn, snapshot_id, *, actor_id=0, notes=None):
        self.ensure_schema(conn)
        record = self._local_snapshot_record(snapshot_id, actor_id=actor_id, notes=notes)
        current = conn.execute("SELECT id FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        if current:
            conn.execute(
                "UPDATE snapshots SET status=?, storage_path=?, db_dump_path=?, files_archive_path=?, "
                "config_archive_path=?, checksum=?, size_bytes=?, completed_at=? WHERE id=?",
                (
                    record["status"],
                    record["storage_path"],
                    record["db_dump_path"],
                    record["files_archive_path"],
                    record["config_archive_path"],
                    record["checksum"],
                    record["size_bytes"],
                    record["completed_at"],
                    snapshot_id,
                ),
            )
            return record
        conn.execute(
            "INSERT INTO snapshots "
            "(id, type, status, created_by, created_at, completed_at, app_version, schema_version, source_mode, "
            "includes_json, storage_path, db_dump_path, files_archive_path, config_archive_path, checksum, size_bytes, notes) "
            "VALUES (:id, :type, :status, :created_by, :created_at, :completed_at, :app_version, :schema_version, "
            ":source_mode, :includes_json, :storage_path, :db_dump_path, :files_archive_path, :config_archive_path, "
            ":checksum, :size_bytes, :notes)",
            record,
        )
        return record

    def _current_mode(self, conn):
        self.ensure_schema(conn)
        row = conn.execute("SELECT current_mode FROM server_modes WHERE id=1").fetchone()
        return row["current_mode"] if row else "test"

    def _actor_id(self, actor):
        return int(dict(actor or {}).get("id") or 0)

    def _actor_name(self, actor):
        return dict(actor or {}).get("username") or "system"

    def _sanitize_database_label(self, label):
        clean = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(label or "").strip()).strip("._-")
        if not clean:
            raise ValueError("additional database label is empty")
        return clean[:80]

    def _normalize_additional_db_paths(self, db_paths):
        if not db_paths:
            return {}
        if isinstance(db_paths, dict):
            items = db_paths.items()
        else:
            items = []
            for item in db_paths:
                if isinstance(item, dict):
                    items.append((item.get("label") or item.get("name"), item.get("path")))
                else:
                    items.append(item)
        normalized = {}
        primary = self.db_path.resolve(strict=False)
        for raw_label, raw_path in items:
            if not raw_path:
                continue
            label = self._sanitize_database_label(raw_label)
            path = Path(raw_path)
            if path.resolve(strict=False) == primary:
                continue
            if label in normalized:
                raise ValueError(f"duplicate additional database label: {label}")
            normalized[label] = path
        return normalized

    def _database_backup_specs(self):
        specs = [
            {
                "label": "primary",
                "path": self.db_path,
                "archive_path": Path("db.sqlite3.backup"),
                "primary": True,
            }
        ]
        seen = {self.db_path.resolve(strict=False)}
        for label, path in self.additional_db_paths.items():
            resolved = path.resolve(strict=False)
            if resolved in seen:
                continue
            seen.add(resolved)
            specs.append(
                {
                    "label": label,
                    "path": path,
                    "archive_path": Path("databases") / f"{label}.sqlite3.backup",
                    "primary": False,
                }
            )
        return specs

    def _write_db_file_backup(self, source, dest):
        source = Path(source)
        dest = Path(dest)
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(str(source))
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(source))
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def _write_db_backup(self, dest):
        self._write_db_file_backup(self.db_path, dest)

    def _write_database_backups(self, snapshot_dir):
        databases = []
        for spec in self._database_backup_specs():
            rel_path = spec["archive_path"].as_posix()
            item = {
                "label": spec["label"],
                "primary": bool(spec["primary"]),
                "source_path": self._rel_to_base_text(spec["path"]),
                "archive_path": rel_path,
                "exists": False,
                "restore_policy": (
                    "forensic_archive_only_append_only_recovery"
                    if spec["label"] in self.PROTECTED_RESTORE_DATABASE_LABELS
                    else "restorable"
                ),
            }
            source = Path(spec["path"])
            if not source.exists():
                if spec["primary"]:
                    raise FileNotFoundError(str(source))
                databases.append(item)
                continue
            if not source.is_file() or source.is_symlink():
                if spec["primary"]:
                    raise RuntimeError(f"primary database path is not a regular file: {source}")
                item["skipped_reason"] = "not_regular_file"
                databases.append(item)
                continue
            backup_path = snapshot_dir / spec["archive_path"]
            self._write_db_file_backup(source, backup_path)
            item.update(
                {
                    "exists": True,
                    "size": backup_path.stat().st_size,
                    "sha256": _sha256_file(backup_path),
                }
            )
            databases.append(item)
        return databases

    def _path_is_snapshot_internal(self, path):
        resolved = Path(path).resolve(strict=False)
        snapshot_root = self.snapshots_root.resolve(strict=False)
        imports_root = self.imports_root.resolve(strict=False)
        return (
            resolved == snapshot_root
            or snapshot_root in resolved.parents
            or resolved == imports_root
            or imports_root in resolved.parents
        )

    def _iter_files(self):
        snapshot_root = self.snapshots_root.resolve(strict=False)
        for root in self.file_roots:
            if not root.exists() or not root.is_dir():
                continue
            root_resolved = root.resolve(strict=False)
            if snapshot_root == root_resolved or snapshot_root in root_resolved.parents:
                continue
            for path in root_resolved.rglob("*"):
                if self._path_is_snapshot_internal(path):
                    continue
                if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts:
                    rel = path.relative_to(self.base_dir.resolve()) if self.base_dir.resolve() in path.resolve().parents else Path(root.name) / path.relative_to(root_resolved)
                    yield path, rel

    def _write_files_archive(self, archive_path):
        manifest = {"files": []}
        with tarfile.open(archive_path, "w:gz") as tar:
            for path, rel in self._iter_files():
                rel_text = str(rel)
                tar.add(path, arcname=rel_text)
                manifest["files"].append({"path": rel_text, "size": path.stat().st_size, "sha256": _sha256_file(path)})
        return manifest

    def _iter_runtime_secret_files(self):
        for cfg in self.runtime_secret_files:
            path = cfg if cfg.is_absolute() else self.base_dir / cfg
            if not path.exists() or not path.is_file() or path.is_symlink():
                continue
            try:
                if (
                    self.runtime_base_dir.resolve(strict=False) != self.base_dir.resolve(strict=False)
                    and (
                        self.runtime_base_dir.resolve(strict=False) in path.resolve(strict=False).parents
                        or path.resolve(strict=False) == self.runtime_base_dir.resolve(strict=False)
                    )
                ):
                    arcname = str(Path("runtime") / path.relative_to(self.runtime_base_dir))
                else:
                    arcname = str(path.relative_to(self.base_dir))
            except Exception:
                arcname = path.name
            yield path, arcname

    def _resolve_runtime_secret_target(self, rel_path):
        rel = Path(str(rel_path or "")).as_posix().strip("/")
        if not rel:
            return None, "invalid_path"
        path_obj = Path(rel)
        if path_obj.parts and path_obj.parts[0] == "runtime":
            target = (self.runtime_base_dir / Path(*path_obj.parts[1:])).resolve(strict=False)
            allowed_base = self.runtime_base_dir.resolve(strict=False)
        else:
            target = (self.base_dir / path_obj).resolve(strict=False)
            allowed_base = self.base_dir.resolve(strict=False)
        if target != allowed_base and allowed_base not in target.parents:
            return None, "outside_runtime_base"
        return target, None

    def _write_config_archive(self, archive_path):
        manifest = {"config_files": [], "runtime_secret_files": []}
        with tarfile.open(archive_path, "w:gz") as tar:
            for cfg in self.config_files:
                if not cfg.exists() or not cfg.is_file():
                    continue
                if cfg.name == ".env":
                    redacted = cfg.parent / ".env.snapshot.redacted"
                    with open(cfg, "r", encoding="utf-8", errors="ignore") as src, open(redacted, "w", encoding="utf-8") as out:
                        for line in src:
                            key = line.split("=", 1)[0].strip()
                            if key and not key.startswith("#"):
                                out.write(f"{key}=<redacted>\n")
                    tar.add(redacted, arcname=".env.snapshot.redacted")
                    manifest["config_files"].append({"path": ".env.snapshot.redacted", "size": redacted.stat().st_size, "sha256": _sha256_file(redacted), "redacted": True})
                    try:
                        redacted.unlink()
                    except Exception:
                        pass
                    continue
                arcname = str(cfg.relative_to(self.base_dir)) if self.base_dir in cfg.resolve().parents else cfg.name
                tar.add(cfg, arcname=arcname)
                manifest["config_files"].append({"path": arcname, "size": cfg.stat().st_size, "sha256": _sha256_file(cfg), "redacted": False})
            for secret_path, arcname in self._iter_runtime_secret_files():
                tar.add(secret_path, arcname=arcname)
                manifest["runtime_secret_files"].append({"path": arcname, "size": secret_path.stat().st_size, "sha256": _sha256_file(secret_path)})
        return manifest

    def _validate_runtime_secret_files(self, snapshot_dir):
        metadata_path = snapshot_dir / "metadata.json"
        if not metadata_path.exists():
            return {"ok": True, "checked": 0, "errors": []}
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "checked": 0, "errors": [{"path": "metadata.json", "reason": f"invalid metadata: {exc}"}]}
        expected = metadata.get("runtime_secret_files") or []
        if not isinstance(expected, list) or not expected:
            return {"ok": True, "checked": 0, "errors": []}
        checked = 0
        errors = []
        for item in expected:
            rel_path = str((item or {}).get("path") or "").strip()
            digest = str((item or {}).get("sha256") or "").strip()
            if not rel_path or not digest:
                continue
            target, error = self._resolve_runtime_secret_target(rel_path)
            if error:
                errors.append({"path": rel_path, "reason": error})
                continue
            checked += 1
            if not target.exists() or not target.is_file():
                errors.append({"path": rel_path, "reason": "missing"})
                continue
            actual = _sha256_file(target)
            if actual != digest:
                errors.append({"path": rel_path, "reason": "hash_mismatch", "expected_sha256": digest, "actual_sha256": actual})
        return {"ok": not errors, "checked": checked, "errors": errors}

    def _load_snapshot_metadata(self, snapshot_dir):
        metadata_path = snapshot_dir / "metadata.json"
        if not metadata_path.exists():
            return {}
        return json.loads(metadata_path.read_text(encoding="utf-8"))

    def _runtime_secret_restore_paths(self, snapshot_dir):
        try:
            metadata = self._load_snapshot_metadata(snapshot_dir)
        except Exception:
            return set()
        expected = metadata.get("runtime_secret_files") or []
        restore_paths = set()
        for item in expected:
            rel_path = str((item or {}).get("path") or "").strip().strip("/")
            if rel_path:
                restore_paths.add(rel_path)
        return restore_paths

    def _apply_staged_config_restore(self, snapshot_dir, stage_dir):
        runtime_secret_paths = self._runtime_secret_restore_paths(snapshot_dir)
        moved = []
        stage_root = Path(stage_dir)
        allowed_base = self.base_dir.resolve(strict=False)
        for staged in sorted(stage_root.rglob("*")):
            if not staged.is_file():
                continue
            rel = staged.relative_to(stage_root).as_posix()
            if rel in runtime_secret_paths:
                target, error = self._resolve_runtime_secret_target(rel)
                if error:
                    raise RuntimeError(f"runtime secret restore blocked: {rel} ({error})")
            else:
                target = (self.base_dir / rel).resolve(strict=False)
                if target != allowed_base and allowed_base not in target.parents:
                    raise RuntimeError(f"config restore blocked outside base_dir: {rel}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if target.is_dir():
                    raise RuntimeError(f"config restore target is a directory: {target}")
                target.unlink()
            os.replace(staged, target)
            moved.append({"path": rel, "target": str(target)})
        try:
            shutil.rmtree(stage_root)
        except Exception:
            pass
        return {"moved": moved}

    def create_snapshot(self, *, snapshot_type, actor, notes=None):
        if snapshot_type not in SNAPSHOT_TYPES:
            return SnapshotResult(False, error="snapshot type 錯誤")
        actor_id = self._actor_id(actor)
        actor_name = self._actor_name(actor)
        snapshot_id = self._snapshot_id()
        snapshot_dir = self._snapshot_dir(snapshot_id)
        created_at = datetime.now().isoformat()
        includes = {
            "database": True,
            "databases": True,
            "file_roots": True,
            "config": True,
            "audit_checkpoint": True,
        }
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            source_mode = self._current_mode(conn)
            snapshot_dir.mkdir(parents=True, exist_ok=False)
            conn.execute(
                "INSERT INTO snapshots "
                "(id, type, status, created_by, created_at, app_version, schema_version, source_mode, includes_json, storage_path, notes) "
                "VALUES (?, ?, 'creating', ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    snapshot_type,
                    actor_id,
                    created_at,
                    APP_RELEASE_ID,
                    str(CURRENT_SCHEMA_VERSION),
                    source_mode,
                    json.dumps(includes, sort_keys=True),
                    str(snapshot_dir),
                    notes or "",
                ),
            )
            conn.commit()
            self.audit("SNAPSHOT_CREATE_STARTED", "-", user=actor_name, success=True, detail=f"snapshot_id={snapshot_id},type={snapshot_type}")

            db_dump = snapshot_dir / "db.sqlite3.backup"
            files_archive = snapshot_dir / "uploads.tar.gz"
            config_archive = snapshot_dir / "config.tar.gz"
            manifest_path = snapshot_dir / "manifest.json"
            checksums_path = snapshot_dir / "checksums.sha256"
            metadata_path = snapshot_dir / "metadata.json"

            database_manifest = self._write_database_backups(snapshot_dir)
            file_manifest = self._write_files_archive(files_archive)
            config_manifest = self._write_config_archive(config_archive)
            manifest = {
                "databases": database_manifest,
                "files": file_manifest.get("files", []),
                "config": config_manifest,
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

            checksums = {}
            for item in database_manifest:
                if not item.get("exists"):
                    continue
                rel_path = str(item.get("archive_path") or "").strip()
                if rel_path:
                    checksums[rel_path] = _sha256_file(snapshot_dir / rel_path)
            for path in (files_archive, config_archive, manifest_path):
                checksums[path.name] = _sha256_file(path)
            checksums_text = "".join(f"{digest}  {name}\n" for name, digest in sorted(checksums.items()))
            checksums_path.write_text(checksums_text, encoding="utf-8")
            overall_checksum = hashlib.sha256(checksums_text.encode("utf-8")).hexdigest()
            metadata = {
                "snapshot_id": snapshot_id,
                "type": snapshot_type,
                "created_by": actor_name,
                "created_at": created_at,
                "app_version": APP_RELEASE_ID,
                "schema_version": str(CURRENT_SCHEMA_VERSION),
                "source_mode": source_mode,
                "includes": includes,
                "database_files": database_manifest,
                "file_roots": [self._rel_to_base_text(root) for root in self.file_roots],
                "secrets_excluded": False,
                "env_redacted": True,
                "runtime_secret_files": config_manifest.get("runtime_secret_files", []),
                "checksum_algorithm": "sha256",
                "checksum": overall_checksum,
                "notes": notes or "",
            }
            metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            size_bytes = sum(p.stat().st_size for p in snapshot_dir.rglob("*") if p.is_file())
            conn.execute(
                "UPDATE snapshots SET status='ready', completed_at=?, db_dump_path=?, files_archive_path=?, "
                "config_archive_path=?, checksum=?, size_bytes=? WHERE id=?",
                (datetime.now().isoformat(), str(db_dump), str(files_archive), str(config_archive), overall_checksum, size_bytes, snapshot_id),
            )
            conn.commit()
            self.audit("SNAPSHOT_CREATE_READY", "-", user=actor_name, success=True, detail=f"snapshot_id={snapshot_id},size={size_bytes}")
            return SnapshotResult(True, snapshot_id=snapshot_id, status="ready", metadata=metadata)
        except Exception as exc:
            try:
                if snapshot_dir.exists():
                    shutil.rmtree(snapshot_dir)
                conn.execute("UPDATE snapshots SET status='failed', error_message=? WHERE id=?", (str(exc), snapshot_id))
                conn.commit()
            except Exception:
                pass
            self.audit("SNAPSHOT_CREATE_FAILED", "-", user=actor_name, success=False, detail=f"snapshot_id={snapshot_id},error={exc}")
            return SnapshotResult(False, snapshot_id=snapshot_id, status="failed", error=str(exc))
        finally:
            conn.close()

    def list_snapshots(self, *, actor):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            rows = conn.execute(
                "SELECT s.id, s.type, s.status, s.created_at, u.username AS created_by, s.size_bytes, s.source_mode, s.notes, s.checksum "
                "FROM snapshots s LEFT JOIN users u ON u.id=s.created_by ORDER BY s.created_at DESC LIMIT 100"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_snapshot(self, *, snapshot_id, actor=None):
        path = self._snapshot_dir(snapshot_id)
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            row = conn.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            metadata_path = path / "metadata.json"
            data["metadata"] = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else None
            return data
        finally:
            conn.close()

    def _verify_snapshot_dir(self, path):
        path = Path(path)
        metadata_path = path / "metadata.json"
        checksums_path = path / "checksums.sha256"
        required = [path / name for name in PORTABLE_SNAPSHOT_FILES]
        missing = [p.name for p in required if not p.exists()]
        if missing:
            return {"ok": False, "msg": "snapshot 檔案缺失", "missing": missing}
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        checksums = {}
        for line in checksums_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            digest, name = line.split(None, 1)
            checksums[name.strip()] = digest
        for name, digest in checksums.items():
            rel = Path(name)
            if rel.is_absolute() or ".." in rel.parts:
                return {"ok": False, "msg": "checksum path 不安全", "file": name}
            target = path / name
            if not target.exists() or _sha256_file(target) != digest:
                return {"ok": False, "msg": "checksum 不一致", "file": name}
        overall = hashlib.sha256(checksums_path.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        if metadata.get("checksum") != overall:
            return {"ok": False, "msg": "metadata checksum 不一致"}
        database_files = metadata.get("database_files")
        if not isinstance(database_files, list) or not database_files:
            database_files = [{"label": "primary", "archive_path": "db.sqlite3.backup", "exists": True}]
        for item in database_files:
            if not (item or {}).get("exists", True):
                continue
            archive_name = str((item or {}).get("archive_path") or "").strip()
            if not archive_name:
                continue
            archive_rel = Path(archive_name)
            if archive_rel.is_absolute() or ".." in archive_rel.parts:
                return {"ok": False, "msg": "database backup path 不安全", "file": archive_name}
            db_backup = path / archive_rel
            if not db_backup.exists():
                return {"ok": False, "msg": "database backup 缺失", "file": archive_name}
            conn = sqlite3.connect(str(db_backup))
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                if not row or str(row[0]).lower() != "ok":
                    return {"ok": False, "msg": "database integrity_check 失敗", "file": archive_name, "result": row[0] if row else None}
            finally:
                conn.close()
        for archive in (path / "uploads.tar.gz", path / "config.tar.gz"):
            with tarfile.open(archive, "r:gz") as tar:
                for member in tar.getmembers():
                    if not _safe_relative_tarinfo(member):
                        return {"ok": False, "msg": "tar 內容包含不安全的成員", "file": member.name}
        return {"ok": True, "msg": "snapshot 驗證通過", "metadata": metadata}

    def verify_snapshot(self, *, snapshot_id):
        return self._verify_snapshot_dir(self._snapshot_dir(snapshot_id))

    def _portable_export_names(self, snapshot_dir):
        names = list(PORTABLE_SNAPSHOT_FILES)
        checksums_path = Path(snapshot_dir) / "checksums.sha256"
        if checksums_path.exists():
            for line in checksums_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    _digest, name = line.split(None, 1)
                except ValueError:
                    continue
                name = name.strip()
                rel = Path(name)
                if rel.is_absolute() or ".." in rel.parts:
                    raise ValueError(f"unsafe snapshot export member: {name}")
                if name not in names:
                    names.append(name)
        return names

    def export_snapshot_archive(self, *, snapshot_id, actor=None):
        actor_name = self._actor_name(actor)
        snapshot = self.get_snapshot(snapshot_id=snapshot_id, actor=actor)
        if not snapshot:
            return {"ok": False, "msg": "找不到 snapshot"}
        if snapshot.get("status") != "ready":
            return {"ok": False, "msg": "snapshot 尚未 ready"}
        verification = self.verify_snapshot(snapshot_id=snapshot_id)
        if not verification["ok"]:
            self.audit("SNAPSHOT_EXPORT_VERIFY_FAILED", "-", user=actor_name, success=False, detail=f"snapshot_id={snapshot_id},reason={verification}")
            return {"ok": False, "msg": verification["msg"], "verification": verification}

        snapshot_dir = self._snapshot_dir(snapshot_id)
        archive_path = self._portable_archive_path(snapshot_id)
        tmp_path = archive_path.with_suffix(archive_path.suffix + ".tmp")
        with tarfile.open(tmp_path, "w:gz") as tar:
            for name in self._portable_export_names(snapshot_dir):
                tar.add(snapshot_dir / name, arcname=f"{snapshot_id}/{name}")
        os.replace(tmp_path, archive_path)
        size_bytes = archive_path.stat().st_size
        self.audit("SNAPSHOT_EXPORTED", "-", user=actor_name, success=True, detail=f"snapshot_id={snapshot_id},size={size_bytes}")
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "path": str(archive_path),
            "filename": archive_path.name,
            "size_bytes": size_bytes,
            "verification": verification,
        }

    def _copy_archive_input(self, *, archive_path=None, file_storage=None, dest=None):
        if archive_path:
            shutil.copyfile(archive_path, dest)
            return
        stream = getattr(file_storage, "stream", file_storage)
        if hasattr(stream, "seek"):
            stream.seek(0)
        with open(dest, "wb") as out:
            shutil.copyfileobj(stream, out)

    def _locate_imported_snapshot_dir(self, import_dir):
        direct = [import_dir / name for name in PORTABLE_SNAPSHOT_FILES]
        if all(path.exists() for path in direct):
            return import_dir
        children = [path for path in import_dir.iterdir() if path.is_dir()]
        matches = [path for path in children if all((path / name).exists() for name in PORTABLE_SNAPSHOT_FILES)]
        if len(matches) != 1:
            raise ValueError("snapshot 封包格式錯誤")
        return matches[0]

    def import_snapshot_archive(self, *, actor, archive_path=None, file_storage=None, notes=None):
        if not archive_path and file_storage is None:
            return {"ok": False, "msg": "缺少 snapshot 檔案"}
        actor_id = self._actor_id(actor)
        actor_name = self._actor_name(actor)
        self.imports_root.mkdir(parents=True, exist_ok=True)
        import_id = f"import_{secrets.token_hex(8)}"
        import_dir = self.imports_root / import_id
        package_path = self.imports_root / f"{import_id}.tar.gz"
        try:
            import_dir.mkdir(parents=True, exist_ok=False)
            self._copy_archive_input(archive_path=archive_path, file_storage=file_storage, dest=package_path)
            if not package_path.exists() or package_path.stat().st_size <= 0:
                raise ValueError("snapshot 檔案為空")
            _safe_extract_tar(package_path, import_dir)
            imported_dir = self._locate_imported_snapshot_dir(import_dir)
            verification = self._verify_snapshot_dir(imported_dir)
            if not verification["ok"]:
                return {"ok": False, "msg": verification["msg"], "verification": verification}
            metadata = verification.get("metadata") or {}
            snapshot_id = metadata.get("snapshot_id")
            if not _safe_snapshot_id(snapshot_id):
                return {"ok": False, "msg": "snapshot metadata id 格式錯誤"}
            target_dir = self._snapshot_dir(snapshot_id)
            if target_dir.exists():
                return {"ok": False, "msg": "本機已存在相同 snapshot_id", "snapshot_id": snapshot_id}
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(imported_dir), str(target_dir))
            conn = self.get_db()
            try:
                record = self._upsert_local_snapshot_record(
                    conn,
                    snapshot_id,
                    actor_id=actor_id,
                    notes=f"imported portable snapshot; {notes or ''}".strip(),
                )
                conn.commit()
            finally:
                conn.close()
            self.audit("SNAPSHOT_IMPORTED", "-", user=actor_name, success=True, detail=f"snapshot_id={snapshot_id},size={record['size_bytes']}")
            return {"ok": True, "snapshot_id": snapshot_id, "snapshot": self.get_snapshot(snapshot_id=snapshot_id, actor=actor), "verification": verification}
        except Exception as exc:
            self.audit("SNAPSHOT_IMPORT_FAILED", "-", user=actor_name, success=False, detail=f"error={exc}")
            return {"ok": False, "msg": "snapshot 匯入失敗", "error": str(exc)}
        finally:
            try:
                if import_dir.exists():
                    shutil.rmtree(import_dir)
                if package_path.exists():
                    package_path.unlink()
            except Exception:
                pass

    def restore_snapshot_archive(self, *, actor, archive_path=None, file_storage=None, reason="", dry_run=False):
        imported = self.import_snapshot_archive(actor=actor, archive_path=archive_path, file_storage=file_storage, notes=reason)
        if not imported.get("ok"):
            return imported
        result = self.restore_snapshot(
            snapshot_id=imported["snapshot_id"],
            actor=actor,
            reason=reason or "restore from uploaded portable snapshot",
            dry_run=dry_run,
        )
        return {**result, "imported_snapshot_id": imported["snapshot_id"], "import": imported}

    def _restore_db(self, snapshot_dir):
        self._restore_db_file(snapshot_dir / "db.sqlite3.backup", self.db_path)

    def _restore_db_file(self, backup_path, target_path):
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(backup_path))
        dst = sqlite3.connect(str(target_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def _restore_additional_dbs(self, snapshot_dir):
        try:
            metadata = self._load_snapshot_metadata(snapshot_dir)
        except Exception:
            metadata = {}
        database_files = metadata.get("database_files") or []
        restored = []
        skipped = []
        for item in database_files:
            if not isinstance(item, dict) or item.get("primary"):
                continue
            label = self._sanitize_database_label(item.get("label"))
            archive_name = str(item.get("archive_path") or "").strip()
            if label in self.PROTECTED_RESTORE_DATABASE_LABELS:
                skipped.append({
                    "label": label,
                    "reason": "append_only_financial_restore_disabled",
                    "archive_path": archive_name,
                })
                continue
            if not item.get("exists"):
                skipped.append({"label": label, "reason": "snapshot_missing_source"})
                continue
            target = self.additional_db_paths.get(label)
            if not target:
                skipped.append({"label": label, "reason": "not_configured"})
                continue
            rel = Path(archive_name)
            if not archive_name or rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError(f"unsafe additional database backup path: {archive_name}")
            backup_path = Path(snapshot_dir) / rel
            if not backup_path.exists():
                raise FileNotFoundError(str(backup_path))
            self._restore_db_file(backup_path, target)
            restored.append({"label": label, "target_path": self._rel_to_base_text(target), "archive_path": archive_name})
        return {"restored": restored, "skipped": skipped}

    def _export_mode_switch_logs(self):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT * FROM mode_switch_logs
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def _merge_mode_switch_logs(self, rows):
        if not rows:
            return {"ok": True, "inserted": 0, "preserved": 0, "chain": {"ok": True, "count": 0}}
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            inserted = 0
            for row in rows:
                exists = conn.execute("SELECT 1 FROM mode_switch_logs WHERE id=?", (row.get("id"),)).fetchone()
                if exists:
                    continue
                conn.execute(
                    """
                    INSERT INTO mode_switch_logs
                    (id, event_uuid, from_mode, to_mode, actor_user_id, actor_id, actor_role, source_ip, user_agent, request_id,
                     reason, checkpoint_id, snapshot_id, success, error_message, config_diff_json, restore_result_json,
                     created_at, prev_hash, row_hash, server_boot_id, hmac_signature, key_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row.get("id"),
                        row.get("event_uuid") or row.get("id"),
                        row.get("from_mode"),
                        row.get("to_mode"),
                        row.get("actor_user_id"),
                        row.get("actor_id") if row.get("actor_id") is not None else row.get("actor_user_id"),
                        row.get("actor_role") or "",
                        row.get("source_ip") or "",
                        row.get("user_agent") or "",
                        row.get("request_id") or "",
                        row.get("reason") or "",
                        row.get("checkpoint_id"),
                        row.get("snapshot_id"),
                        int(row.get("success") or 0),
                        row.get("error_message") or "",
                        row.get("config_diff_json") or "{}",
                        row.get("restore_result_json") or "{}",
                        row.get("created_at") or datetime.now().isoformat(),
                        row.get("prev_hash") or "",
                        row.get("row_hash") or "",
                        row.get("server_boot_id") or "",
                        row.get("hmac_signature") or "",
                        row.get("key_version") or "",
                    ),
                )
                inserted += 1
            chain = verify_mode_switch_log_hash_chain(conn)
            conn.commit()
            return {"ok": bool(chain.get("ok")), "inserted": inserted, "preserved": len(rows), "chain": chain}
        except Exception as exc:
            conn.rollback()
            return {"ok": False, "inserted": 0, "preserved": len(rows), "error": str(exc)}
        finally:
            conn.close()

    def _clear_file_roots(self):
        for root in self.file_roots:
            if root.exists() and root.is_dir():
                for child in root.iterdir():
                    if self._path_is_snapshot_internal(child):
                        continue
                    if child.is_symlink():
                        child.unlink()
                    elif child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            root.mkdir(parents=True, exist_ok=True)

    def _rel_to_base_text(self, path):
        try:
            return str(Path(path).resolve(strict=False).relative_to(self.base_dir.resolve(strict=False)))
        except Exception:
            return str(path)

    def _remove_runtime_secret_files(self):
        removed = []
        skipped = []
        allowed_base = self.runtime_base_dir.resolve(strict=False)
        for raw_path in self.runtime_secret_files:
            path = raw_path if raw_path.is_absolute() else self.base_dir / raw_path
            rel_text = self._rel_to_base_text(path)
            try:
                resolved = path.resolve(strict=False)
                if resolved != allowed_base and allowed_base not in resolved.parents:
                    skipped.append({"path": str(path), "reason": "outside_runtime_base"})
                    continue
                if not path.exists() and not path.is_symlink():
                    continue
                if path.is_dir():
                    skipped.append({"path": rel_text, "reason": "is_directory"})
                    continue
                path.unlink()
                removed.append(rel_text)
            except Exception as exc:
                skipped.append({"path": rel_text, "reason": str(exc)})
        return {"removed": removed, "skipped": skipped}

    def _relocate_runtime_secret_files_from_restore(self, snapshot_dir):
        metadata_path = snapshot_dir / "metadata.json"
        if not metadata_path.exists():
            return {"moved": [], "skipped": []}
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"moved": [], "skipped": [{"path": "metadata.json", "reason": f"invalid metadata: {exc}"}]}
        expected = metadata.get("runtime_secret_files") or []
        moved = []
        skipped = []
        for item in expected:
            rel_path = str((item or {}).get("path") or "").strip()
            if not rel_path:
                continue
            staged = (self.base_dir / rel_path).resolve(strict=False)
            target, error = self._resolve_runtime_secret_target(rel_path)
            if error:
                skipped.append({"path": rel_path, "reason": error})
                continue
            if staged == target:
                continue
            if not staged.exists() or not staged.is_file():
                skipped.append({"path": rel_path, "reason": "staged_missing"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target.unlink()
            os.replace(staged, target)
            moved.append({"path": rel_path, "target": str(target)})
            try:
                parent = staged.parent
                while parent != self.base_dir and parent.exists():
                    parent.rmdir()
                    parent = parent.parent
            except Exception:
                pass
        return {"moved": moved, "skipped": skipped}

    def _existing_resettable_tables(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        existing = {row["name"] if isinstance(row, sqlite3.Row) else row[0] for row in rows}
        reset_tables = existing & RESETTABLE_TABLES
        priority = {
            "forum_post_reactions": 10,
            "forum_thread_reactions": 11,
            "forum_posts": 12,
            "forum_threads": 13,
            "forum_boards": 14,
            "forum_categories": 15,
            "album_files": 20,
            "albums": 21,
            "storage_share_links": 30,
            "file_access_grants": 31,
            "encrypted_file_keys": 32,
            "cloud_file_refs": 33,
            "storage_files": 34,
            "storage_folders": 35,
            "uploaded_files": 36,
            "direct_messages": 40,
            "dm_threads": 41,
            "chat_message_reports": 42,
            "chat_messages": 43,
            "trading_spot_realized_pnl": 50,
            "trading_fills": 51,
            "trading_orders": 52,
            "trading_spot_positions": 53,
            "trading_futures_positions": 54,
            "trading_margin_positions": 55,
            "trading_pending_profit": 56,
            "trading_reserve_pool_events": 57,
            "trading_audit_events": 58,
            "trading_reserve_pool": 59,
            "trading_state": 60,
            "trading_markets": 61,
        }
        return sorted(reset_tables, key=lambda name: (priority.get(name, 100), name))

    def _apply_management_only_settings(self, conn, *, actor_name, reset_at):
        applied = {}
        for key, value in MANAGEMENT_ONLY_RESET_SETTINGS.items():
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
                (key, str(bool(value)), reset_at, actor_name or "system_reset"),
            )
            applied[key] = bool(value)
        row = conn.execute("SELECT current_mode FROM server_modes WHERE id=1").fetchone()
        previous_mode = row["current_mode"] if row else None
        conn.execute(
            """
            INSERT INTO server_modes
            (id, current_mode, previous_mode, active_snapshot_id, mode_changed_by, mode_changed_at, notes)
            VALUES (1, 'dev_ready', ?, NULL, NULL, ?, 'runtime reset default')
            ON CONFLICT(id) DO UPDATE SET
                previous_mode=excluded.previous_mode,
                current_mode='dev_ready',
                active_snapshot_id=NULL,
                mode_changed_by=NULL,
                mode_changed_at=excluded.mode_changed_at,
                notes=excluded.notes
            """,
            (previous_mode, reset_at),
        )
        return applied

    def daily_snapshot_status(self, *, settings, now=None):
        now = now or datetime.now()
        settings = dict(settings or {})
        enabled_raw = settings.get("snapshot_daily_auto_enabled", False)
        enabled = enabled_raw if isinstance(enabled_raw, bool) else str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}
        hour, minute, normalized_time = _parse_daily_snapshot_time(settings.get("snapshot_daily_time"))
        today = now.date().isoformat()
        last_date = str(settings.get("snapshot_daily_last_date") or "")
        due_at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        due = enabled and last_date != today and now >= due_at
        reason = "due"
        if not enabled:
            reason = "disabled"
        elif last_date == today:
            reason = "already_created_today"
        elif now < due_at:
            reason = "before_scheduled_time"
        return {
            "enabled": enabled,
            "configured_time": normalized_time,
            "today": today,
            "last_date": last_date,
            "due": due,
            "reason": reason,
            "due_at": due_at.isoformat(),
            "checked_at": now.isoformat(),
        }

    def create_daily_snapshot_if_due(self, *, actor, settings, save_settings=None, now=None, force=False, notes=None):
        now = now or datetime.now()
        status = self.daily_snapshot_status(settings=settings, now=now)
        if not force and not status["due"]:
            return {"ok": True, "created": False, "status": status}

        result = self.create_snapshot(
            snapshot_type="scheduled",
            actor=actor,
            notes=notes or f"daily auto snapshot {status['today']}",
        )
        if not result.ok:
            return {
                "ok": False,
                "created": False,
                "msg": "daily snapshot 建立失敗",
                "error": result.error,
                "status": status,
            }
        if save_settings:
            save_settings({"snapshot_daily_last_date": status["today"]})
        return {
            "ok": True,
            "created": True,
            "snapshot_id": result.snapshot_id,
            "status": {**status, "last_date": status["today"], "due": False, "reason": "created"},
        }

    def reset_runtime_state(self, *, actor, confirm, reason):
        if confirm != "RESET_RUNTIME_STATE":
            return {"ok": False, "msg": "confirm 必須等於 RESET_RUNTIME_STATE"}

        actor_id = self._actor_id(actor)
        actor_name = self._actor_name(actor)
        pre = self.create_snapshot(snapshot_type="pre_reset", actor=actor, notes=f"Before runtime reset: {reason or ''}")
        if not pre.ok:
            return {"ok": False, "msg": "pre_reset snapshot failed", "error": pre.error}

        reset_at = datetime.now().isoformat()
        conn = self.get_db()
        cleared_tables = []
        try:
            self.ensure_schema(conn)
            for table in self._existing_resettable_tables(conn):
                conn.execute(f"DELETE FROM {table}")
                cleared_tables.append(table)
            if cleared_tables:
                try:
                    placeholders = ",".join("?" for _ in cleared_tables)
                    conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", cleared_tables)
                except Exception:
                    pass
            management_settings = self._apply_management_only_settings(conn, actor_name=actor_name, reset_at=reset_at)
            conn.commit()
        finally:
            conn.close()

        self._clear_file_roots()
        points_result = None
        if self.reset_points_chain:
            points_result = self.reset_points_chain(
                actor=actor,
                reason=reason or "",
                pre_reset_snapshot_id=pre.snapshot_id,
            )
        secret_result = self._remove_runtime_secret_files()
        audit_detail = (
            f"actor_id={actor_id},pre_reset_snapshot={pre.snapshot_id},tables={','.join(cleared_tables)},"
            f"points_chain_reset={bool(points_result and points_result.get('ok'))},"
            f"server_mode=test,"
            f"runtime_secret_files_removed={','.join(secret_result['removed'])},"
            f"runtime_secret_files_skipped={json.dumps(secret_result['skipped'], ensure_ascii=False, sort_keys=True)},"
            f"management_only_settings={','.join(k for k, v in management_settings.items() if v)},"
            f"disabled_settings={','.join(k for k, v in management_settings.items() if not v)},"
            f"reason={reason or ''},reset_at={reset_at}"
        )
        audit_result = None
        if self.reset_audit_chain:
            try:
                audit_result = self.reset_audit_chain(
                    "SYSTEM_RUNTIME_RESET",
                    "-",
                    user=actor_name,
                    success=True,
                    detail=audit_detail,
                    write_event=False,
                )
            except TypeError:
                audit_result = self.reset_audit_chain(
                    "SYSTEM_RUNTIME_RESET",
                    "-",
                    user=actor_name,
                    success=True,
                    detail=audit_detail,
                )
        else:
            self.audit("SYSTEM_RUNTIME_RESET", "-", user=actor_name, success=True, detail=audit_detail)
        return {
            "ok": True,
            "msg": "Runtime 狀態已重置",
            "pre_reset_snapshot_id": pre.snapshot_id,
            "cleared_tables": cleared_tables,
            "points_chain_reset": points_result,
            "audit_chain_reset": audit_result,
            "server_mode": "dev_ready",
            "management_only_settings": management_settings,
            "runtime_secret_files_removed": secret_result["removed"],
            "runtime_secret_files_skipped": secret_result["skipped"],
            "requires_restart": True,
            "reset_at": reset_at,
        }

    def restore_snapshot(self, *, snapshot_id, actor, reason, dry_run=False):
        actor_id = self._actor_id(actor)
        actor_name = self._actor_name(actor)
        verification = self.verify_snapshot(snapshot_id=snapshot_id)
        if not verification["ok"]:
            self.audit("SNAPSHOT_VERIFY_FAILED", "-", user=actor_name, success=False, detail=f"snapshot_id={snapshot_id},reason={verification}")
            return {"ok": False, "msg": verification["msg"], "verification": verification}
        self.audit("SNAPSHOT_VERIFY_OK", "-", user=actor_name, success=True, detail=f"snapshot_id={snapshot_id}")
        if dry_run:
            event_id = f"restore_{secrets.token_hex(8)}"
            conn = self.get_db()
            try:
                self.ensure_schema(conn)
                conn.execute(
                    "INSERT INTO snapshot_restore_events "
                    "(id, snapshot_id, restored_by, started_at, completed_at, status, restore_mode, checksum_verified, dry_run, error_message) "
                    "VALUES (?, ?, ?, ?, ?, 'verified', 'dry_run', 1, 1, NULL)",
                    (event_id, snapshot_id, actor_id, datetime.now().isoformat(), datetime.now().isoformat()),
                )
                conn.commit()
            finally:
                conn.close()
            return {"ok": True, "msg": "dry-run verified", "event_id": event_id, "verification": verification}

        active_restore = self.restore_in_progress()
        if active_restore:
            return {
                "ok": False,
                "msg": "已有 Snapshot 還原任務進行中",
                "restore_in_progress": active_restore,
            }

        pre = self.create_snapshot(snapshot_type="pre_restore", actor=actor, notes=f"Before restore {snapshot_id}: {reason}")
        if not pre.ok:
            return {"ok": False, "msg": "pre_restore snapshot failed", "error": pre.error}
        preserved_mode_switch_logs = self._export_mode_switch_logs()
        event_id = f"restore_{secrets.token_hex(8)}"
        started_at = datetime.now().isoformat()
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            settings_cols = {
                row["name"] for row in conn.execute("PRAGMA table_info(system_settings)").fetchall()
            }
            # 舊版 runtime 只有 key/value/updated_at/updated_by，沒有 value_type。
            # restore 準備階段不能因為 schema 尚未升級就卡死；這裡只在欄位存在時補 type。
            if "value_type" in settings_cols:
                conn.execute(
                    "UPDATE system_settings SET value='true', value_type='bool', updated_at=? WHERE key='maintenance_mode'",
                    (started_at,),
                )
            else:
                conn.execute(
                    "UPDATE system_settings SET value='true', updated_at=? WHERE key='maintenance_mode'",
                    (started_at,),
                )
            conn.execute(
                "INSERT INTO snapshot_restore_events "
                "(id, snapshot_id, restored_by, started_at, status, restore_mode, pre_restore_snapshot_id, checksum_verified, dry_run) "
                "VALUES (?, ?, ?, ?, 'restoring', 'full', ?, 1, 0)",
                (event_id, snapshot_id, actor_id, started_at, pre.snapshot_id),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO snapshot_restore_events "
                    "(id, snapshot_id, restored_by, started_at, completed_at, status, restore_mode, pre_restore_snapshot_id, checksum_verified, dry_run, error_message) "
                    "VALUES (?, ?, ?, ?, ?, 'failed', 'prepare', ?, 1, 0, ?)",
                    (event_id, snapshot_id, actor_id, started_at, datetime.now().isoformat(), pre.snapshot_id, str(exc)),
                )
                conn.commit()
            except Exception:
                conn.rollback()
            self.audit(
                "SNAPSHOT_RESTORE_PREPARE_FAILED",
                "-",
                user=actor_name,
                success=False,
                detail=f"snapshot_id={snapshot_id},pre_restore={pre.snapshot_id},error={exc}",
            )
            return {
                "ok": False,
                "msg": "還原準備失敗",
                "error": str(exc),
                "pre_restore_snapshot_id": pre.snapshot_id,
            }
        finally:
            conn.close()

        staged_snapshot_parent = None
        try:
            snapshot_dir = self._snapshot_dir(snapshot_id)
            restore_source, staged_snapshot_parent = self._stage_snapshot_dir_for_restore(snapshot_dir)
            self.audit("SNAPSHOT_RESTORE_STARTED", "-", user=actor_name, success=True, detail=f"snapshot_id={snapshot_id},pre_restore={pre.snapshot_id},reason={reason}")
            self._restore_db(restore_source)
            database_restore = self._restore_additional_dbs(restore_source)
            self._clear_file_roots()
            _safe_extract_tar(restore_source / "uploads.tar.gz", self.base_dir)
            config_restore_stage = restore_source / ".restore_config_stage"
            if config_restore_stage.exists():
                shutil.rmtree(config_restore_stage)
            config_restore_stage.mkdir(parents=True, exist_ok=True)
            _safe_extract_tar(restore_source / "config.tar.gz", config_restore_stage)
            self._apply_staged_config_restore(restore_source, config_restore_stage)
            completed_at = datetime.now().isoformat()
            mode_log_merge = self._merge_mode_switch_logs(preserved_mode_switch_logs)
            conn = self.get_db()
            try:
                self.ensure_schema(conn)
                self._upsert_local_snapshot_record(conn, snapshot_id, actor_id=actor_id)
                conn.execute(
                    "INSERT OR REPLACE INTO snapshot_restore_events "
                    "(id, snapshot_id, restored_by, started_at, completed_at, status, restore_mode, pre_restore_snapshot_id, checksum_verified, dry_run) "
                    "VALUES (?, ?, ?, ?, ?, 'completed', 'full', ?, 1, 0)",
                    (event_id, snapshot_id, actor_id, started_at, completed_at, pre.snapshot_id),
                )
                conn.commit()
            finally:
                conn.close()
            if not mode_log_merge.get("ok"):
                self.audit("MODE_SWITCH_LOG_RESTORE_PRESERVE_FAILED", "-", user=actor_name, success=False, detail=json.dumps(mode_log_merge, ensure_ascii=False, sort_keys=True))
                return {
                    "ok": False,
                    "msg": "還原後 mode-switch log 保留失敗",
                    "event_id": event_id,
                    "pre_restore_snapshot_id": pre.snapshot_id,
                    "mode_switch_log_merge": mode_log_merge,
                }
            runtime_secret_validation = self._validate_runtime_secret_files(restore_source)
            if not runtime_secret_validation["ok"]:
                conn = self.get_db()
                try:
                    self.ensure_schema(conn)
                    conn.execute(
                        "INSERT OR REPLACE INTO snapshot_restore_events "
                        "(id, snapshot_id, restored_by, started_at, completed_at, status, restore_mode, pre_restore_snapshot_id, checksum_verified, dry_run, error_message) "
                        "VALUES (?, ?, ?, ?, ?, 'failed', 'full', ?, 1, 0, ?)",
                        (
                            event_id,
                            snapshot_id,
                            actor_id,
                            started_at,
                            datetime.now().isoformat(),
                            pre.snapshot_id,
                            json.dumps(runtime_secret_validation, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
                self.audit(
                    "SNAPSHOT_RESTORE_RUNTIME_SECRETS_FAILED",
                    "-",
                    user=actor_name,
                    success=False,
                    detail=f"snapshot_id={snapshot_id},runtime_secrets={json.dumps(runtime_secret_validation, ensure_ascii=False, sort_keys=True)}",
                )
                return {
                    "ok": False,
                    "msg": "Runtime 密鑰驗證失敗",
                    "event_id": event_id,
                    "pre_restore_snapshot_id": pre.snapshot_id,
                    "runtime_secret_validation": runtime_secret_validation,
                    "requires_restart": True,
                }
            post_restore_validation = self._run_post_restore_validators()
            if not post_restore_validation["ok"]:
                conn = self.get_db()
                try:
                    self.ensure_schema(conn)
                    conn.execute(
                        "INSERT OR REPLACE INTO snapshot_restore_events "
                        "(id, snapshot_id, restored_by, started_at, completed_at, status, restore_mode, pre_restore_snapshot_id, checksum_verified, dry_run, error_message) "
                        "VALUES (?, ?, ?, ?, ?, 'failed', 'full', ?, 1, 0, ?)",
                        (
                            event_id,
                            snapshot_id,
                            actor_id,
                            started_at,
                            datetime.now().isoformat(),
                            pre.snapshot_id,
                            json.dumps(post_restore_validation, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
                self.audit("SNAPSHOT_RESTORE_VALIDATION_FAILED", "-", user=actor_name, success=False, detail=f"snapshot_id={snapshot_id},validation={post_restore_validation}")
                return {
                    "ok": False,
                    "msg": "Restore 後驗證失敗",
                    "event_id": event_id,
                    "pre_restore_snapshot_id": pre.snapshot_id,
                    "post_restore_validation": post_restore_validation,
                    "runtime_secret_validation": runtime_secret_validation,
                    "database_restore": database_restore,
                    "requires_restart": True,
                }
            self.audit("SNAPSHOT_RESTORE_COMPLETED", "-", user=actor_name, success=True, detail=f"snapshot_id={snapshot_id},pre_restore={pre.snapshot_id},reason={reason}")
            return {
                "ok": True,
                "msg": "Snapshot 已還原",
                "event_id": event_id,
                "pre_restore_snapshot_id": pre.snapshot_id,
                "post_restore_validation": post_restore_validation,
                "runtime_secret_validation": runtime_secret_validation,
                "database_restore": database_restore,
                "requires_restart": True,
            }
        except Exception as exc:
            conn = self.get_db()
            try:
                self.ensure_schema(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO snapshot_restore_events "
                    "(id, snapshot_id, restored_by, started_at, completed_at, status, restore_mode, pre_restore_snapshot_id, checksum_verified, dry_run, error_message) "
                    "VALUES (?, ?, ?, ?, ?, 'failed', 'full', ?, 1, 0, ?)",
                    (event_id, snapshot_id, actor_id, started_at, datetime.now().isoformat(), pre.snapshot_id, str(exc)),
                )
                conn.commit()
            finally:
                conn.close()
            self.audit("SNAPSHOT_RESTORE_FAILED", "-", user=actor_name, success=False, detail=f"snapshot_id={snapshot_id},error={exc}")
            return {"ok": False, "msg": "還原失敗", "error": str(exc), "pre_restore_snapshot_id": pre.snapshot_id}
        finally:
            if staged_snapshot_parent and staged_snapshot_parent.exists():
                try:
                    shutil.rmtree(staged_snapshot_parent)
                except Exception:
                    pass

    def delete_snapshot(self, *, snapshot_id, actor, reason):
        path = self._snapshot_dir(snapshot_id)
        actor_name = self._actor_name(actor)
        if path.exists():
            shutil.rmtree(path)
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.execute("UPDATE snapshots SET status='deleted', error_message=? WHERE id=?", (reason or "", snapshot_id))
            conn.commit()
        finally:
            conn.close()
        self.audit("SNAPSHOT_DELETE", "-", user=actor_name, success=True, detail=f"snapshot_id={snapshot_id},reason={reason}")
        return {"ok": True, "msg": "Snapshot 已刪除"}
