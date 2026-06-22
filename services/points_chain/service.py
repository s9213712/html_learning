"""PointsChain ledger, wallet, and verification service."""

import time as _monotonic_time
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from . import schema as _schema
from .economy_layer import (
    EXCHANGE_PRINCIPAL_LENT_TYPES,
    EXCHANGE_PRINCIPAL_RECEIVABLE_ADDRESS_PREFIX,
    EXCHANGE_PRINCIPAL_REPAID_TYPES,
    append_economy_event,
    bootstrap_economy_layer,
    economy_fund_address,
    ensure_economy_layer_schema,
    exchange_principal_receivable_address,
    economy_layer_report,
    economy_supply_equation_report,
    load_economy_policy,
    replay_economy_events,
)
from .wallet_identity import (
    WALLET_ADDRESS_RE,
    address_dispute_payload,
    canonical_public_jwk,
    create_official_hot_wallet,
    ensure_wallet_identity_schema,
    get_primary_wallet_identity,
    is_pc0_internal_address,
    list_wallet_identities,
    official_hot_wallet_address,
    verify_wallet_address_dispute_signature,
    verify_wallet_service_fee_signature,
    verify_wallet_transaction_signature,
)

globals().update({name: value for name, value in _schema.__dict__.items() if not name.startswith("__")})

EXPLORER_FINALITY_PROVED_COUNT = 20
EXPLORER_BASE_FINALITY_MIN_SECONDS = 120
EXPLORER_BASE_FINALITY_MAX_SECONDS = 180
EXPLORER_ACCELERATED_FINALITY_MIN_SECONDS = 30
EXPLORER_ACCELERATED_FINALITY_MAX_SECONDS = 45
EXPLORER_ACCELERATION_REFERENCE_FEE_POINTS = 20
EXPLORER_MAX_ACCELERATION_FEE_POINTS = 10000
EXPLORER_CHAIN_HUMAN_RULE = "20 Proved；ETA 依鏈上忙碌度與 priority fee 動態估算"
EXPLORER_INTERNAL_HOT_WALLET_HUMAN_RULE = "pc0 Inner Address 使用站內帳本即時成交；免 20 Proved、免 priority fee"
# Legacy read-only compatibility for service-fee reserve rows created before
# pc0 internal service payments became immediate. New service payments must not
# wait for this threshold.
LEGACY_SERVICE_BATCH_SETTLEMENT_MIN_POINTS = 100
ROOT_TRANSACTION_LIST_SWEEP_LIMIT = 5
SERVICE_FEE_REVENUE_DESTINATION_FUND = "official_treasury"
NON_OPERATING_MINT_SOURCE_FUND = "mint"
TRANSFER_SETTLEMENT_RAILS = {
    "internal_hot_wallet",
    "internal_system_burn",
    "cold_chain",
    "deposit_bridge_credit",
    "withdrawal_bridge_lock",
    "withdrawal_bridge_broadcast",
    "withdrawal_bridge_confirm",
    "withdrawal_bridge_refund",
}
PC0_OPERATIONAL_SETTLEMENT_RAILS = {
    "internal_hot_wallet",
    "internal_system_burn",
    "deposit_bridge_credit",
    "withdrawal_bridge_lock",
    "withdrawal_bridge_refund",
}
PC1_CANONICAL_SETTLEMENT_RAILS = {
    "cold_chain",
    "withdrawal_bridge_broadcast",
    "withdrawal_bridge_confirm",
}
GOVERNANCE_CLOCK_JUMP_TOLERANCE_SECONDS = 30
GOVERNANCE_CLOCK_DRIFT_TOLERANCE_RATIO = 0.10
GOVERNANCE_CLOCK_DRIFT_TOLERANCE_MAX_SECONDS = 5 * 60
WALLET_CREATION_FEE_BASE_POINTS = 25
WALLET_CREATION_FEE_MULTIPLIER = 2
WALLET_CREATION_FEE_MAX_POINTS = 100_000
PROVISIONAL_ADDRESS_FREEZE_SECONDS = 24 * 60 * 60
DISPUTE_INITIAL_FREEZE_SECONDS = 60 * 60
DISPUTE_ESCALATED_FREEZE_SECONDS = 24 * 60 * 60
DISPUTE_VOTING_GRACE_SECONDS = 10 * 60
DISPUTE_FROM_DAILY_LIMIT = 3
GOV_RATE_UNIT_SUFFIX = "b" + "ps"
GOV_QUORUM_RATE_FIELD = "quorum_" + GOV_RATE_UNIT_SUFFIX
GOV_PASS_THRESHOLD_RATE_FIELD = "pass_threshold_" + GOV_RATE_UNIT_SUFFIX
GOV_YES_THRESHOLD_RATE_FIELD = "yes_threshold_" + GOV_RATE_UNIT_SUFFIX
GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD = "vote_differential_required_" + GOV_RATE_UNIT_SUFFIX
GOV_VOTE_DIFFERENTIAL_RATE_FIELD = "vote_differential_" + GOV_RATE_UNIT_SUFFIX
GOV_APPROVAL_RATE_FIELD = "approval_" + GOV_RATE_UNIT_SUFFIX
GOV_QUORUM_DELTA_RATE_FIELD = "quorum_delta_" + GOV_RATE_UNIT_SUFFIX
GOV_DILUTION_RATE_FIELD = "dilution_" + GOV_RATE_UNIT_SUFFIX


def _metadata_bool(value, *, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"0", "false", "no", "off", ""}:
        return False
    if text in {"1", "true", "yes", "on"}:
        return True
    return bool(default)


def _connection_path(conn):
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        return str(row["file"] if hasattr(row, "keys") else row[2])
    except Exception:
        return ""


def _is_duplicate_column_error(exc):
    return isinstance(exc, sqlite3.OperationalError) and "duplicate column name" in str(exc).lower()


def _governance_monotonic_seconds():
    """Use a suspend-aware clock when the platform exposes one."""
    clock_id = getattr(_monotonic_time, "CLOCK_BOOTTIME", None)
    clock_gettime = getattr(_monotonic_time, "clock_gettime", None)
    if clock_id is not None and callable(clock_gettime):
        try:
            return float(clock_gettime(clock_id))
        except Exception:
            pass
    return float(_monotonic_time.monotonic())


class PointsLedgerService:
    _schema_lock = threading.Lock()
    _schema_ready_paths = set()
    _transaction_dispute_schema_lock = threading.Lock()
    _transaction_dispute_schema_ready_paths = set()
    _wallet_lock = threading.Lock()

    def __init__(self, *, get_db, chain_secret, audit=None, backup_dir=None, mode_reader=None, security_event_recorder=None):
        self.get_db = get_db
        self.chain_secret = chain_secret
        self.audit = audit or (lambda *args, **kwargs: None)
        self.backup_dir = Path(backup_dir or os.environ.get("POINTS_CHAIN_BACKUP_DIR") or "./secure_backups/points_chain")
        # Phase 7: production-only guard. mode_reader is injected so
        # this module stays decoupled from server / Flask. Default
        # behavior when no reader is configured: refuse the write —
        # this is intentional. A non-configured reader means we can't
        # prove we're in production, and PointsChain MUST default to
        # safe-locked, never to production-routing.
        self._mode_reader = mode_reader
        self._security_event_recorder = security_event_recorder or (lambda *a, **kw: None)
        self._governance_clock_lock = threading.Lock()
        self._governance_clock_wall = datetime.now(timezone.utc)
        self._governance_clock_monotonic = _governance_monotonic_seconds()
        self._governance_clock_last_error = ""
        self._observability_lock = threading.Lock()
        self._last_transfer_compact_sweep = {}

    def rc1_facade(self):
        from .wallet_facade import WalletServiceFacade

        return WalletServiceFacade(points_service=self)

    def ensure_schema(self, conn):
        db_path = _connection_path(conn)
        if db_path and db_path in self._schema_ready_paths:
            return
        with self._schema_lock:
            if db_path and db_path in self._schema_ready_paths:
                return
            ensure_points_economy_schema(conn)
            ensure_wallet_identity_schema(conn)
            ensure_economy_layer_schema(conn)
            if db_path:
                self._schema_ready_paths.add(db_path)

    def _public_account_id(self, user_id):
        return public_account_id(self.chain_secret, user_id)

    def _node_fingerprint(self):
        return sha256_text(f"pointschain-node:{self.chain_secret}")

    def _sign_block(self, block):
        hmac_key = str(self.chain_secret or "").encode("utf-8")
        return hmac.new(hmac_key, block_signature_payload(block).encode("utf-8"), hashlib.sha256).hexdigest()

    def _sign_backup_manifest(self, manifest_without_signature):
        hmac_key = f"points-chain-backup:{self.chain_secret or ''}".encode("utf-8")
        return hmac.new(hmac_key, canonical_json(manifest_without_signature).encode("utf-8"), hashlib.sha256).hexdigest()

    def _backup_root(self):
        root = self.backup_dir.expanduser()
        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except Exception:
            pass
        for name in ("backups", "forensics"):
            path = root / name
            path.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(path, 0o700)
            except Exception:
                pass
        return root

    def _safe_mode_row(self, conn):
        return conn.execute("SELECT * FROM points_chain_recovery_state WHERE id=1").fetchone()

    def _safe_mode_status(self, conn):
        row = self._safe_mode_row(conn)
        if not row:
            return {"safe_mode": False}
        plan = _json_loads(row["restore_plan_json"], {})
        return {
            "safe_mode": bool(row["safe_mode"]),
            "reason": row["reason"] or "",
            "forensic_bundle_id": row["forensic_bundle_id"],
            "restore_plan": plan,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "restored_at": row["restored_at"],
            "verification": _json_loads(row["verification_json"], {}),
        }

    def safe_mode_status(self):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            return self._safe_mode_status(conn)
        finally:
            conn.close()

    def _assert_chain_writable(self, conn, action):
        row = self._safe_mode_row(conn)
        if row and int(row["safe_mode"] or 0):
            raise ValueError(f"PointsChain safe mode active; {action} is paused until branch/governance recovery resolves the incident")

    def _governance_assert_clock_safe_locked(self, conn, action):
        """Reject governance writes if wall time jumps faster than monotonic time.

        This prevents a live process from accepting an artificially advanced
        system clock as proof that a timelock or voting deadline has elapsed.
        It is intentionally local-process protection; post-RC1 external anchors
        and host time monitoring are still required for full host compromise.
        """
        wall_now = datetime.now(timezone.utc)
        monotonic_now = _governance_monotonic_seconds()
        with self._governance_clock_lock:
            previous_wall = self._governance_clock_wall
            previous_monotonic = self._governance_clock_monotonic
            wall_elapsed = (wall_now - previous_wall).total_seconds()
            monotonic_elapsed = monotonic_now - previous_monotonic
            forward_jump = wall_elapsed - max(0.0, monotonic_elapsed)
            backward_jump = -wall_elapsed
            elapsed_basis = max(abs(wall_elapsed), abs(monotonic_elapsed), 1.0)
            drift_tolerance = min(
                GOVERNANCE_CLOCK_DRIFT_TOLERANCE_MAX_SECONDS,
                max(GOVERNANCE_CLOCK_JUMP_TOLERANCE_SECONDS, elapsed_basis * GOVERNANCE_CLOCK_DRIFT_TOLERANCE_RATIO),
            )
            violation = ""
            if forward_jump > drift_tolerance:
                violation = "wall_clock_fast_forward"
            elif backward_jump > drift_tolerance:
                violation = "wall_clock_moved_backward"
            if violation:
                details = {
                    "action": str(action or ""),
                    "violation": violation,
                    "wall_now": wall_now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    "previous_wall": previous_wall.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
                    "wall_elapsed_seconds": round(wall_elapsed, 3),
                    "monotonic_elapsed_seconds": round(monotonic_elapsed, 3),
                    "tolerance_seconds": round(drift_tolerance, 3),
                    "base_tolerance_seconds": GOVERNANCE_CLOCK_JUMP_TOLERANCE_SECONDS,
                    "drift_tolerance_ratio": GOVERNANCE_CLOCK_DRIFT_TOLERANCE_RATIO,
                    "drift_tolerance_max_seconds": GOVERNANCE_CLOCK_DRIFT_TOLERANCE_MAX_SECONDS,
                    "guard_model": "wall_clock_vs_monotonic_v1",
                }
                self._governance_clock_last_error = canonical_json(details)
                self._enter_safe_mode(conn, details, "governance_clock_jump_detected")
                conn.commit()
                raise ValueError("governance clock guard active: system clock jump detected")
            self._governance_clock_wall = wall_now
            self._governance_clock_monotonic = monotonic_now
            self._governance_clock_last_error = ""

    def _assert_production_mode(self, action):
        """Phase 7 guard for block sealing modes.

        Reads the mode every call (no caching) so a switch out of
        an allowed sealing mode immediately blocks subsequent writes. If the mode
        reader is unavailable or raises, we still refuse the write —
        chain integrity outweighs convenience. The violation is
        logged as a `chain_mode_violation` security event so the
        attempt is visible even when the caller swallows the
        exception.
        """
        mode = None
        try:
            if callable(self._mode_reader):
                mode = self._mode_reader()
        except Exception:
            mode = None
        if mode not in {"production", "dev_ready"}:
            try:
                self._security_event_recorder(
                    "chain_mode_violation",
                    target_user="-",
                    detail=f"action={action},mode={mode!r}",
                )
            except Exception:
                pass
            raise ChainModeViolation(mode, action=action)

    def _main_branch_uuid(self):
        return "main"

    def _canonical_branch_uuid(self, conn):
        try:
            row = conn.execute(
                """
                SELECT branch_uuid
                FROM points_chain_governance_branches
                WHERE is_canonical=1 AND write_enabled=1
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            if row and row["branch_uuid"]:
                return str(row["branch_uuid"])
        except Exception:
            pass
        return self._main_branch_uuid()

    def _branch_metadata(self, conn, branch_uuid=None):
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid()).strip() or self._main_branch_uuid()
        if branch == self._main_branch_uuid():
            row = conn.execute(
                """
                SELECT COUNT(*) AS archived_count
                FROM points_chain_governance_branches
                WHERE parent_branch_uuid='' AND status IN ('archived', 'read_only_archived')
                """
            ).fetchone()
            archived = bool(row and int(row["archived_count"] or 0))
            return {
                "branch_uuid": branch,
                "branch_name": "main",
                "parent_branch_uuid": "",
                "is_canonical": not archived,
                "write_enabled": not archived,
                "status": "read_only_archived" if archived else "canonical_main",
                "asset_universe": "main",
            }
        row = conn.execute(
            "SELECT * FROM points_chain_governance_branches WHERE branch_uuid=?",
            (branch,),
        ).fetchone()
        if not row:
            return {
                "branch_uuid": branch,
                "branch_name": branch,
                "parent_branch_uuid": "",
                "is_canonical": False,
                "write_enabled": False,
                "status": "unknown",
                "asset_universe": branch,
            }
        return {
            "branch_uuid": row["branch_uuid"],
            "branch_name": row["branch_name"],
            "parent_branch_uuid": row["parent_branch_uuid"],
            "is_canonical": bool(row["is_canonical"]),
            "write_enabled": bool(row["write_enabled"]),
            "status": row["status"],
            "asset_universe": row["branch_uuid"],
            "recovery_type": row["recovery_type"] if "recovery_type" in row.keys() else "canonical_pointer_only",
            "activated_at": row["activated_at"],
        }

    def _assert_canonical_write_branch(self, conn, branch_uuid):
        branch = str(branch_uuid or self._main_branch_uuid()).strip() or self._main_branch_uuid()
        canonical = self._canonical_branch_uuid(conn)
        if branch != canonical:
            raise PermissionError("chain branch is no longer canonical; old-branch assets are read-only and cannot be spent on the active branch")
        meta = self._branch_metadata(conn, branch)
        if not meta.get("write_enabled", True):
            raise PermissionError("chain branch is read-only and cannot accept new transactions")
        return branch

    def _wallet_totals_from_ledger(self, rows, *, branch_uuid=None, strict=True):
        totals = {}
        branch_filter = str(branch_uuid or "").strip()
        for row in rows:
            if branch_filter and str(row["chain_branch"] if "chain_branch" in row.keys() else "main") != branch_filter:
                continue
            user_id = int(row["user_id"])
            item = totals.setdefault(user_id, {
                "balance": 0,
                "frozen": 0,
                "earned": 0,
                "spent": 0,
            })
            amount = int(row["amount"])
            direction = row["direction"]
            public_metadata = _json_loads(row["public_metadata_json"], {})
            earned_delta, spent_delta = self._wallet_statement_deltas(
                direction=direction,
                amount=amount,
                action_type=row["action_type"],
                public_metadata=public_metadata,
            )
            if direction in {"credit", "transfer_in"}:
                item["balance"] += amount
            elif direction in {"debit", "transfer_out", "reverse"}:
                item["balance"] -= amount
            elif direction == "freeze":
                item["balance"] -= amount
                item["frozen"] += amount
            elif direction == "unfreeze":
                item["balance"] += amount
                item["frozen"] -= amount
            item["earned"] += earned_delta
            item["spent"] += spent_delta
            if strict and (item["balance"] < 0 or item["frozen"] < 0):
                raise ValueError(f"ledger replay would create negative wallet for user {user_id}")
        return totals

    def _wallet_statement_totals_for_user(self, conn, user_id):
        branch = self._canonical_branch_uuid(conn)
        rows = conn.execute(
            """
            SELECT * FROM points_ledger
            WHERE user_id=? AND chain_branch=?
            ORDER BY id ASC
            """,
            (int(user_id), branch),
        ).fetchall()
        strict_replay = not self._wallet_identity_has_history(conn, user_id)
        return self._wallet_totals_from_ledger(rows, branch_uuid=branch, strict=strict_replay).get(int(user_id), {
            "balance": 0,
            "frozen": 0,
            "earned": 0,
            "spent": 0,
        })

    def _wallet_identity_adjusted_totals(self, conn, user_id, total):
        adjusted = dict(total or {"balance": 0, "frozen": 0, "earned": 0, "spent": 0})
        try:
            identity_balances = self._wallet_identity_balances_for_user(conn, user_id, include_pending=False)
        except Exception:
            return adjusted
        if not identity_balances.get("has_identity"):
            return adjusted
        balances = identity_balances.get("balances") or {}
        adjusted["balance"] = sum(int(item.get("balance") or 0) for item in balances.values())
        adjusted["frozen"] = sum(int(item.get("frozen") or 0) for item in balances.values())
        return adjusted

    def _wallet_identity_has_history(self, conn, user_id):
        try:
            return bool(self._wallet_identity_state_for_user(conn, user_id).get("has_identity"))
        except Exception:
            return False

    def _rebuild_wallets_from_ledger(self, conn):
        started_transaction = False
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
            started_transaction = True
        try:
            branch = self._canonical_branch_uuid(conn)
            rows = conn.execute("SELECT * FROM points_ledger WHERE chain_branch=? ORDER BY id ASC", (branch,)).fetchall()
            totals = self._wallet_totals_from_ledger(rows, branch_uuid=branch, strict=False)
            now = utc_now()
            existing = {
                int(row["user_id"]): dict(row)
                for row in conn.execute("SELECT * FROM points_wallets").fetchall()
            }
            conn.execute("DELETE FROM points_wallets")
            user_ids = {int(row["id"]) for row in conn.execute("SELECT id FROM users").fetchall()}
            user_ids.update(totals.keys())
            for user_id in sorted(user_ids):
                old = existing.get(user_id, {})
                total = totals.get(user_id, {"balance": 0, "frozen": 0, "earned": 0, "spent": 0})
                has_identity = self._wallet_identity_has_history(conn, user_id)
                total = self._wallet_identity_adjusted_totals(conn, user_id, total)
                if not has_identity and (int(total.get("balance") or 0) < 0 or int(total.get("frozen") or 0) < 0):
                    raise ValueError(f"ledger replay would create negative wallet for user {user_id}")
                conn.execute(
                    """
                    INSERT INTO points_wallets (
                        user_id, soft_balance, hard_balance, soft_frozen, hard_frozen,
                        total_soft_earned, total_hard_earned, total_soft_spent, total_hard_spent,
                        wallet_status, risk_level, created_at, updated_at
                    ) VALUES (?, ?, 0, ?, 0, ?, 0, ?, 0, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        int(total["balance"]),
                        int(total["frozen"]),
                        int(total["earned"]),
                        int(total["spent"]),
                        old.get("wallet_status") or "active",
                        old.get("risk_level") or "normal",
                        old.get("created_at") or now,
                        now,
                    ),
                )
            if started_transaction:
                conn.commit()
            return {"wallets_rebuilt": len(user_ids), "source_ledger_rows": len(rows)}
        except Exception:
            if started_transaction:
                conn.rollback()
            raise

    def _verify_wallets_against_ledger(self, conn):
        branch = self._canonical_branch_uuid(conn)
        rows = conn.execute("SELECT * FROM points_ledger WHERE chain_branch=? ORDER BY id ASC", (branch,)).fetchall()
        totals = self._wallet_totals_from_ledger(rows, branch_uuid=branch)
        wallet_rows = {
            int(row["user_id"]): row
            for row in conn.execute("SELECT * FROM points_wallets ORDER BY user_id ASC").fetchall()
        }
        user_ids = set(wallet_rows.keys())
        user_ids.update(totals.keys())
        user_ids.update(int(row["id"]) for row in conn.execute("SELECT id FROM users").fetchall())
        errors = []
        for user_id in sorted(user_ids):
            wallet = wallet_rows.get(user_id)
            expected = totals.get(user_id, {"balance": 0, "frozen": 0, "earned": 0, "spent": 0})
            expected = self._wallet_identity_adjusted_totals(conn, user_id, expected)
            if not wallet:
                if any(int(expected[key]) for key in ("balance", "frozen", "earned", "spent")):
                    repairable_derived_cache = self._wallet_identity_has_history(conn, user_id)
                    errors.append({
                        "type": "wallet_missing",
                        "severity": "medium" if repairable_derived_cache else "critical",
                        "message": f"wallet for user #{user_id} is missing but ledger has balance data",
                        "user_id": user_id,
                        "expected": expected,
                        "repairable_derived_cache": repairable_derived_cache,
                    })
                continue
            comparisons = {
                "soft_balance": int(expected["balance"]),
                "soft_frozen": int(expected["frozen"]),
                "total_soft_earned": int(expected["earned"]),
                "total_soft_spent": int(expected["spent"]),
                "hard_balance": 0,
                "hard_frozen": 0,
                "total_hard_earned": 0,
                "total_hard_spent": 0,
            }
            mismatches = []
            for column, expected_value in comparisons.items():
                actual_value = int(wallet[column] or 0)
                if actual_value != expected_value:
                    mismatches.append({"field": column, "expected": expected_value, "actual": actual_value})
            if mismatches:
                repairable_derived_cache = self._wallet_identity_has_history(conn, user_id)
                errors.append({
                    "type": "wallet_ledger_mismatch",
                    "severity": "medium" if repairable_derived_cache else "critical",
                    "message": f"wallet for user #{user_id} does not match ledger replay",
                    "user_id": user_id,
                    "mismatches": mismatches,
                    "repairable_derived_cache": repairable_derived_cache,
                })
        return errors

    def restore_from_backup(self, *, actor, backup_id, confirm):
        raise PermissionError(
            "PointsChain ledger backup restore is disabled. Use safe mode, "
            "forensic bundles, recovery branches, emergency governance, and "
            "append-only correction transactions instead of overwriting ledger history."
        )

    def _ensure_local_node(self, conn):
        now = utc_now()
        fingerprint = self._node_fingerprint()
        conn.execute(
            """
            INSERT OR IGNORE INTO points_chain_nodes (
                node_id, node_name, node_type, public_key, public_key_fingerprint,
                enabled, created_at, updated_at
            ) VALUES ('single-node', 'Local PointsChain node', 'local_hmac', ?, ?, 1, ?, ?)
            """,
            (fingerprint, fingerprint, now, now),
        )

    def _backfill_missing_block_signatures(self, conn):
        self._ensure_local_node(conn)
        now = utc_now()
        blocks = conn.execute(
            """
            SELECT b.*
            FROM points_chain_blocks b
            LEFT JOIN points_chain_block_signatures s ON s.block_id=b.id AND s.node_id='single-node'
            WHERE s.id IS NULL
            ORDER BY b.block_number ASC
            """
        ).fetchall()
        for block in blocks:
            if compute_block_hash(block) != block["block_hash"]:
                continue
            conn.execute(
                """
                INSERT INTO points_chain_block_signatures (
                    block_id, node_id, signature_algorithm, public_key_fingerprint, signature, signed_at
                ) VALUES (?, 'single-node', 'hmac-sha256', ?, ?, ?)
                """,
                (block["id"], self._node_fingerprint(), self._sign_block(block), now),
            )

    def _audit_log(self, conn, event_type, severity, message, *, actor=None, target_user_id=None, ledger_id=None, block_id=None, metadata=None):
        conn.execute(
            """
            INSERT INTO points_chain_audit_logs (
                event_type, severity, actor_user_id, actor_role, target_user_id,
                related_ledger_id, related_block_id, message, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                severity,
                int(actor_value(actor, "id")) if actor_value(actor, "id") else None,
                actor_value(actor, "role"),
                target_user_id,
                ledger_id,
                block_id,
                message,
                _json_dumps(metadata or {}),
                utc_now(),
            ),
        )

    def reset_runtime_chain(self, *, actor=None, reason="", pre_reset_snapshot_id=""):
        """Reset the live PointsChain runtime state after a full server reset.

        The pre-reset server snapshot remains the recovery source. The active
        ledger, blocks, wallets, pending rewards, disputes, recovery state, and
        daily stats are cleared so the next startup can bootstrap a fresh
        genesis allocation. A PointsChain audit entry is left as the first event
        in the new chain audit log.
        """
        conn = self.get_db()
        reset_tables = [
            "points_chain_block_signatures",
            "points_chain_blocks",
            "points_chain_governance_audit_log",
            "points_chain_governance_votes",
            "points_chain_address_risk_labels",
            "points_chain_address_freezes",
            "points_chain_address_provisional_freezes",
            "points_chain_governance_branches",
            "points_chain_governance_proposals",
            "points_disputes",
            "points_ledger",
            "points_wallets",
            "points_wallet_identity_balances",
            "points_wallet_identity_balance_state",
            "points_pending_rewards",
            "points_economy_daily_stats",
            "points_chain_recovery_state",
            "points_chain_backup_catalog",
            "points_chain_audit_logs",
        ]
        backup_files = {"removed": [], "skipped": []}
        try:
            self.ensure_schema(conn)
            before = self._chain_head_summary(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DROP TRIGGER IF EXISTS trg_points_ledger_no_delete")
            conn.execute("DROP TRIGGER IF EXISTS trg_points_ledger_core_immutable")
            conn.execute("DROP TRIGGER IF EXISTS trg_points_chain_blocks_no_update")
            conn.execute("DROP TRIGGER IF EXISTS trg_points_chain_blocks_no_delete")
            conn.execute("DROP TRIGGER IF EXISTS trg_points_chain_block_signatures_no_update")
            conn.execute("DROP TRIGGER IF EXISTS trg_points_chain_block_signatures_no_delete")
            conn.execute("DROP TRIGGER IF EXISTS trg_points_chain_governance_audit_no_update")
            conn.execute("DROP TRIGGER IF EXISTS trg_points_chain_governance_audit_no_delete")
            for table in reset_tables:
                conn.execute(f"DELETE FROM {table}")
            try:
                placeholders = ",".join("?" for _ in reset_tables)
                conn.execute(f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})", reset_tables)
            except Exception:
                pass
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_points_ledger_no_delete
                BEFORE DELETE ON points_ledger
                BEGIN
                    SELECT RAISE(ABORT, 'points ledger is append-only');
                END
                """
            )
            create_points_ledger_immutable_trigger(conn)
            create_points_chain_block_immutable_triggers(conn)
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_points_chain_governance_audit_no_update
                BEFORE UPDATE ON points_chain_governance_audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'governance audit log is append-only');
                END
                """
            )
            conn.execute(
                """
                CREATE TRIGGER IF NOT EXISTS trg_points_chain_governance_audit_no_delete
                BEFORE DELETE ON points_chain_governance_audit_log
                BEGIN
                    SELECT RAISE(ABORT, 'governance audit log is append-only');
                END
                """
            )
            self._audit_log(
                conn,
                "POINTS_CHAIN_RESET",
                "warning",
                "PointsChain reset during server runtime reset",
                actor=actor,
                metadata={
                    "reason": reason or "",
                    "pre_reset_snapshot_id": pre_reset_snapshot_id or "",
                    "before": before,
                    "reset_tables": reset_tables,
                },
            )
            backup_root = self.backup_dir.expanduser()
            backup_path = backup_root / "backups"
            if str(backup_path) not in {"", "/"} and backup_path.exists():
                try:
                    shutil.rmtree(backup_path)
                    backup_files["removed"].append(str(backup_path))
                except Exception as exc:
                    backup_files["skipped"].append({"path": str(backup_path), "reason": str(exc)})
            self._backup_root()
            conn.commit()
            return {
                "ok": True,
                "reset": True,
                "before": before,
                "cleared_tables": reset_tables,
                "backup_files": backup_files,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _public_chain_audit_metadata(self, value):
        if isinstance(value, dict):
            sanitized = {}
            for key, item in value.items():
                if key == "user_id":
                    try:
                        sanitized["public_account_id"] = self._public_account_id(int(item))
                    except Exception:
                        sanitized["public_account_id"] = ""
                    continue
                if key in {"actor_username", "target_username", "username"}:
                    continue
                sanitized[key] = self._public_chain_audit_metadata(item)
            return sanitized
        if isinstance(value, list):
            return [self._public_chain_audit_metadata(item) for item in value]
        return value

    def list_chain_audit_logs(self, *, limit=100):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT * FROM points_chain_audit_logs
                ORDER BY id DESC LIMIT ?
                """,
                (min(200, max(1, int(limit or 100))),),
            ).fetchall()
            logs = []
            for row in rows:
                item = {key: row[key] for key in row.keys() if key != "metadata_json"}
                item["message"] = public_currency_text(item.get("message") or "")
                item["metadata"] = public_currency_payload(
                    self._public_chain_audit_metadata(_json_loads(row["metadata_json"], {}))
                )
                logs.append(item)
            return logs
        finally:
            conn.close()

    def _governance_mode(self):
        try:
            return self._mode_reader() if callable(self._mode_reader) else ""
        except Exception:
            return ""

    def _governance_is_production(self):
        return self._governance_mode() == "production"

    def _governance_actor_role(self, actor):
        username = str(actor_value(actor, "username") or "")
        role = str(actor_value(actor, "role") or "user")
        if username == "root":
            return "super_admin"
        return role

    def _governance_role_rank(self, role):
        return {"user": 0, "manager": 1, "super_admin": 2}.get(str(role or "user"), 0)

    def _governance_is_manager_actor(self, actor):
        return self._governance_role_rank(self._governance_actor_role(actor)) >= self._governance_role_rank("manager")

    def _governance_member_level_rank(self, level):
        return {
            "suspended": -2,
            "restricted": -1,
            "newbie": 0,
            "normal": 1,
            "trusted": 2,
            "vip": 3,
        }.get(str(level or "normal").strip().lower(), 1)

    def _governance_actor_effective_level(self, conn, actor):
        for key in ("effective_level", "member_level", "base_level"):
            value = str(actor_value(actor, key) or "").strip().lower()
            if value:
                return value
        user_id = int(actor_value(actor, "id") or 0)
        if user_id <= 0:
            return "normal"
        try:
            user_cols = table_columns(conn, "users")
            available = [key for key in ("effective_level", "member_level", "base_level") if key in user_cols]
            if not available:
                return "normal"
            row = conn.execute(
                f"SELECT {', '.join(available)} FROM users WHERE id=?",
                (user_id,),
            ).fetchone()
            if not row:
                return "normal"
            for key in available:
                value = str(row[key] or "").strip().lower()
                if value:
                    return value
        except Exception:
            return "normal"
        return "normal"

    def _governance_public_proposer_allowed(self, conn, actor):
        return self._governance_member_level_rank(self._governance_actor_effective_level(conn, actor)) >= self._governance_member_level_rank("trusted")

    def _governance_domain_for_action(self, action_type, default="PUBLIC_COMMON_INTEREST"):
        action_type = str(action_type or "").strip().upper()
        if action_type in {"MINT_REQUEST", "TREASURY_TRANSFER", "EXCHANGE_FUND_REPLENISH", "CONTEST_REWARD_PAYOUT", "TREASURY_SIGNER_CHANGE"}:
            return "OFFICIAL_TREASURY"
        if action_type in {"EMERGENCY_LOCKDOWN", "ROLLBACK_BRANCH"}:
            return "EMERGENCY_SECURITY"
        if action_type in {"PARAMETER_CHANGE", "FEATURE_ACTIVATION", "HARD_FORK_ACCEPTANCE"}:
            return "PROTOCOL_PARAMETER"
        return str(default or "PUBLIC_COMMON_INTEREST").strip().upper()

    def _governance_legacy_type_for_action(self, action_type, conn=None):
        action_type = str(action_type or "").strip().upper()
        if action_type == "MARK_SCAM":
            proposal_type = "scam_address_label"
        elif action_type == "FREEZE_ADDRESS":
            proposal_type = "freeze_wallet_address"
        elif action_type == "UNFREEZE_ADDRESS":
            proposal_type = "unfreeze_wallet_address"
        elif action_type == "ROLLBACK_BRANCH":
            proposal_type = "emergency_recovery_branch"
        elif action_type in {"MINT_REQUEST", "TREASURY_TRANSFER", "EXCHANGE_FUND_REPLENISH", "CONTEST_REWARD_PAYOUT", "TREASURY_SIGNER_CHANGE"}:
            proposal_type = "official_treasury_operation"
        elif action_type == "EMERGENCY_LOCKDOWN":
            proposal_type = "emergency_security_action"
        elif action_type in {"PARAMETER_CHANGE", "FEATURE_ACTIVATION", "HARD_FORK_ACCEPTANCE", "AUTO_BURN_POLICY"}:
            proposal_type = "protocol_parameter_change"
        else:
            proposal_type = "admin_policy_action"
        if conn is not None:
            try:
                sql = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='points_chain_governance_proposals'"
                ).fetchone()
                ddl = str(sql["sql"] if hasattr(sql, "keys") else sql[0]) if sql else ""
                if proposal_type not in ddl and "emergency_recovery_branch" in ddl:
                    return "emergency_recovery_branch"
            except Exception:
                pass
        return proposal_type

    def _governance_policy(self, proposal_type=None, *, governance_domain=None, action_type=None):
        action_type = str(action_type or "").strip().upper()
        domain = str(governance_domain or self._governance_domain_for_action(action_type)).strip().upper()
        if domain == "OFFICIAL_TREASURY":
            return {
                "domain": "OFFICIAL_TREASURY",
                "voter_scope": "manager_plus",
                "root_veto_allowed": True,
                GOV_QUORUM_RATE_FIELD: 6000,
                "minimum_quorum": 2,
                GOV_PASS_THRESHOLD_RATE_FIELD: 6000,
                GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD: 1,
                "timelock_seconds": 12 * 60 * 60 if self._governance_is_production() else 0,
                "expires_seconds": 7 * 24 * 60 * 60,
            }
        if domain == "EMERGENCY_SECURITY":
            return {
                "domain": "EMERGENCY_SECURITY",
                "voter_scope": "manager_plus",
                "root_veto_allowed": False,
                GOV_QUORUM_RATE_FIELD: 5000,
                "minimum_quorum": 2,
                GOV_PASS_THRESHOLD_RATE_FIELD: 8000,
                GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD: 1,
                "timelock_seconds": 0,
                "expires_seconds": 24 * 60 * 60,
            }
        if action_type in {"MARK_SCAM", "FREEZE_ADDRESS", "UNFREEZE_ADDRESS", "AUTO_BURN_POLICY"} or domain == "PUBLIC_COMMON_INTEREST":
            return {
                "domain": "PUBLIC_COMMON_INTEREST",
                "voter_scope": "active_users",
                "root_veto_allowed": False,
                GOV_QUORUM_RATE_FIELD: 3000,
                "minimum_quorum": 5,
                GOV_PASS_THRESHOLD_RATE_FIELD: 6667,
                GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD: 1000,
                "timelock_seconds": 24 * 60 * 60 if self._governance_is_production() else 0,
                "expires_seconds": 7 * 24 * 60 * 60,
            }
        if domain == "PROTOCOL_PARAMETER":
            return {
                "domain": "PROTOCOL_PARAMETER",
                "voter_scope": "active_users",
                "root_veto_allowed": False,
                GOV_QUORUM_RATE_FIELD: 3000,
                "minimum_quorum": 5,
                GOV_PASS_THRESHOLD_RATE_FIELD: 6667,
                GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD: 1000,
                "timelock_seconds": 48 * 60 * 60 if self._governance_is_production() else 0,
                "expires_seconds": 10 * 24 * 60 * 60,
            }
        if domain == "ADMIN_POLICY":
            return {
                "domain": "ADMIN_POLICY",
                "voter_scope": "manager_plus",
                "root_veto_allowed": True,
                GOV_QUORUM_RATE_FIELD: 6000,
                "minimum_quorum": 2,
                GOV_PASS_THRESHOLD_RATE_FIELD: 6000,
                GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD: 1,
                "timelock_seconds": 12 * 60 * 60 if self._governance_is_production() else 0,
                "expires_seconds": 7 * 24 * 60 * 60,
            }
        raise ValueError("unsupported governance proposal type")

    def _governance_policy_for_payload(self, policy, *, action_type, payload):
        action_type = str(action_type or "").strip().upper()
        execution_class = str((payload or {}).get("execution_class") or "").strip().upper()
        proposal_kind = str((payload or {}).get("proposal_type") or "").strip().upper()
        if action_type == "PARAMETER_CHANGE" and (
            execution_class == "MONETARY_POLICY_AMENDMENT"
            or proposal_kind == "SUPPLY_EXPANSION_REQUEST"
        ):
            # The CRITICAL severity delta is applied later, bringing quorum to
            # 50% while preserving an 80% yes threshold and 50% vote differential.
            return {
                **policy,
                "voter_scope": "active_users",
                "root_veto_allowed": False,
                GOV_QUORUM_RATE_FIELD: 3000,
                "minimum_quorum": 5,
                GOV_PASS_THRESHOLD_RATE_FIELD: 8000,
                GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD: 5000,
                "timelock_seconds": 7 * 24 * 60 * 60,
                "expires_seconds": 14 * 24 * 60 * 60,
                "monetary_policy_amendment": True,
            }
        return policy

    def _governance_severity_policy(self, severity):
        severity = str(severity or "NORMAL").strip().upper()
        if severity not in {"LOW", "NORMAL", "HIGH", "CRITICAL"}:
            raise ValueError("unsupported proposal severity")
        return {
            "LOW": {GOV_QUORUM_DELTA_RATE_FIELD: -500, "timelock_multiplier": 1, "deposit_points": 50},
            "NORMAL": {GOV_QUORUM_DELTA_RATE_FIELD: 0, "timelock_multiplier": 1, "deposit_points": 100},
            "HIGH": {GOV_QUORUM_DELTA_RATE_FIELD: 1000, "timelock_multiplier": 2, "deposit_points": 250},
            "CRITICAL": {GOV_QUORUM_DELTA_RATE_FIELD: 2000, "timelock_multiplier": 1, "deposit_points": 500},
        }[severity]

    def _active_governance_voter_ids(self, conn, *, voter_scope="active_users"):
        user_cols = table_columns(conn, "users")
        where = []
        if "status" in user_cols:
            where.append("COALESCE(status, 'active')='active'")
        if "deleted_at" in user_cols:
            where.append("deleted_at IS NULL")
        if str(voter_scope or "") == "manager_plus":
            if "username" in user_cols and "role" in user_cols:
                where.append("(username='root' OR role IN ('manager', 'super_admin'))")
            elif "role" in user_cols:
                where.append("role IN ('manager', 'super_admin')")
        sql = "SELECT id FROM users"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sorted(int(row["id"]) for row in conn.execute(sql).fetchall())

    def _governance_quorum_count(self, eligible_count, policy):
        eligible_count = max(0, int(eligible_count or 0))
        if eligible_count <= 0:
            return 1
        quorum = (eligible_count * int(policy[GOV_QUORUM_RATE_FIELD]) + 9999) // 10000
        minimum = int(policy["minimum_quorum"])
        if eligible_count >= minimum:
            quorum = max(quorum, minimum)
        else:
            quorum = eligible_count
        if eligible_count >= 2:
            quorum = max(2, quorum)
        return max(1, min(eligible_count, quorum))

    def _governance_time_after(self, seconds):
        return datetime.fromtimestamp(time.time() + int(seconds or 0), timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def _governance_evidence(self, evidence):
        if evidence is None:
            return []
        if isinstance(evidence, str):
            evidence = [evidence]
        if not isinstance(evidence, list):
            raise ValueError("evidence must be a list")
        items = []
        for item in evidence[:20]:
            value = str(item or "").strip()
            if value:
                items.append(value[:240])
        return items

    def _governance_require_root(self, actor):
        if actor_value(actor, "username") != "root":
            raise PermissionError("只有 root 可以建立或執行 PointsChain governance proposal")

    def _governance_require_proposer(self, actor, governance_domain):
        if not actor_value(actor, "id"):
            raise PermissionError("login required")
        domain = str(governance_domain or "").strip().upper()
        if domain in {"OFFICIAL_TREASURY", "EMERGENCY_SECURITY", "ADMIN_POLICY"} and not self._governance_is_manager_actor(actor):
            raise PermissionError("manager+ required for this governance proposal")
        if domain == "PROTOCOL_PARAMETER" and not self._governance_is_manager_actor(actor):
            raise PermissionError("manager+ required for protocol governance proposal")

    def _governance_proposer_authority(self, conn, *, actor, governance_domain, action_type, severity, target_address=""):
        user_id = int(actor_value(actor, "id") or 0)
        if user_id <= 0:
            raise PermissionError("login required")
        domain = str(governance_domain or "").strip().upper()
        is_manager = self._governance_is_manager_actor(actor)
        if domain in {"OFFICIAL_TREASURY", "EMERGENCY_SECURITY", "ADMIN_POLICY"}:
            if not is_manager:
                raise PermissionError("manager+ required for this governance proposal")
            return {"sponsor_required": False, "deposit_points": 0, "deposit_status": "not_required", "authority": "manager_plus"}
        if domain == "PROTOCOL_PARAMETER":
            if is_manager:
                return {"sponsor_required": False, "deposit_points": 0, "deposit_status": "not_required", "authority": "manager_plus"}
            raise PermissionError("manager+ required for protocol governance proposal in RC1")
        # PUBLIC_COMMON_INTEREST: manager+ can directly propose. Normal users
        # can file a proposal, but it stays in REVIEW until manager+ sponsors it.
        if is_manager:
            return {"sponsor_required": False, "deposit_points": 0, "deposit_status": "not_required", "authority": "manager_plus_public"}
        if not self._governance_public_proposer_allowed(conn, actor):
            raise PermissionError("trusted member level or manager+ required for public governance proposal")
        recent = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM points_chain_governance_proposals
            WHERE proposer_user_id=? AND created_at>=?
            """,
            (
                user_id,
                datetime.fromtimestamp(time.time() - 24 * 60 * 60, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            ),
        ).fetchone()
        if int(recent["c"] or 0) >= 3:
            raise ValueError("proposal rate limit exceeded: maximum 3 public proposals per 24h")
        duplicate = conn.execute(
            """
            SELECT proposal_uuid FROM points_chain_governance_proposals
            WHERE governance_domain=? AND action_type=? AND target_address=?
              AND lifecycle_status IN ('REVIEW', 'VOTING', 'SUCCEEDED', 'QUEUED', 'TIMELOCKED')
            ORDER BY id DESC LIMIT 1
            """,
            (domain, str(action_type or "").strip().upper(), str(target_address or "")),
        ).fetchone()
        if duplicate:
            raise ValueError("similar active proposal already exists")
        severity_policy = self._governance_severity_policy(severity)
        return {
            "sponsor_required": True,
            "deposit_points": int(severity_policy["deposit_points"]),
            "deposit_status": "not_required",
            "authority": "public_review_requires_sponsor",
        }

    def _governance_require_executor(self, actor):
        if not self._governance_is_manager_actor(actor):
            raise PermissionError("manager+ required to execute PointsChain governance proposal")

    def _governance_begin_immediate(self, conn):
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")

    def _governance_execution_payload_hash(self, *, action_type, governance_domain, target_wallet_address="", target_address="", target_branch="", requested_amount=0, requested_asset="points", payload=None):
        return sha256_text(canonical_json({
            "action_type": str(action_type or "").strip().upper(),
            "governance_domain": str(governance_domain or "").strip().upper(),
            "target_wallet_address": str(target_wallet_address or "").strip().lower(),
            "target_address": str(target_address or "").strip().lower(),
            "target_branch": str(target_branch or "").strip(),
            "requested_amount": int(requested_amount or 0),
            "requested_asset": str(requested_asset or "points").strip().lower(),
            "payload": payload or {},
        }))

    def _governance_multisig_signing_payload(self, row):
        return {
            "action": "points_governance_multisig_sign",
            "proposal_uuid": row["proposal_uuid"],
            "governance_domain": row["governance_domain"],
            "action_type": row["action_type"],
            "target_wallet_address": row["target_wallet_address"],
            "requested_amount": int(row["requested_amount"] or 0),
            "requested_asset": row["requested_asset"],
            "execution_payload_hash": row["execution_payload_hash"],
        }

    def _governance_multisig_policy_locked(self, conn, *, fund_key="official_treasury"):
        rows = conn.execute(
            """
            SELECT u.id AS user_id, u.username, u.role, w.address, w.wallet_type, w.custody_mode,
                   w.public_key_hash, w.metadata_json, w.created_at
            FROM users u
            JOIN points_wallet_identities w ON w.user_id=u.id
            WHERE COALESCE(u.status, 'active')='active'
              AND (u.username='root' OR u.role IN ('manager', 'super_admin'))
              AND w.status IN ('pending_backup', 'active')
            ORDER BY CASE WHEN u.username='root' THEN 0 WHEN u.role='super_admin' THEN 1 WHEN u.role='manager' THEN 2 ELSE 3 END,
                     u.id ASC, w.is_primary DESC, w.id ASC
            """
        ).fetchall()
        seen_users = set()
        signers = []
        for row in rows:
            user_id = int(row["user_id"])
            if user_id in seen_users:
                continue
            seen_users.add(user_id)
            role = "super_admin" if row["username"] == "root" else row["role"]
            signer_weight = 3 if row["username"] == "root" else (2 if role == "super_admin" else 1)
            metadata = _json_loads(row["metadata_json"], {})
            device_id = str(metadata.get("device_id") or metadata.get("signer_device_id") or "").strip()
            if not device_id:
                device_id = "wallet:" + sha256_text(f"{user_id}:{row['address']}")[:16]
            signers.append({
                "user_id": user_id,
                "username": row["username"],
                "role": role,
                "signer_id": "treasury-signer:" + sha256_text(f"{user_id}:{row['address']}")[:20],
                "wallet_address": row["address"],
                "wallet_type": row["wallet_type"],
                "custody_mode": row["custody_mode"],
                "weight": signer_weight,
                "device_id": device_id,
                "pubkey_fingerprint": str(row["public_key_hash"] or "")[:24],
                "signer_created_at": row["created_at"],
                "last_used_at": str(metadata.get("last_signer_used_at") or ""),
                "revoked": False,
            })
        if len(signers) < 2:
            raise ValueError("official treasury multisig requires at least two manager+ signer wallets")
        total_weight = sum(int(item.get("weight") or 0) for item in signers)
        threshold = max(2, (len(signers) * 60 + 99) // 100)
        threshold = min(len(signers), threshold)
        threshold_weight = max(1, (total_weight * 60 + 99) // 100)
        return {
            "policy_version": "OFFICIAL_TREASURY_MULTISIG_V1",
            "fund_key": str(fund_key or "official_treasury"),
            "wallet_type": "official_treasury_multisig",
            "wallet_scope": "official_treasury",
            "custody_mode": "multisig",
            "spend_capability": "enabled",
            "threshold": threshold,
            "threshold_weight": threshold_weight,
            "signer_count": len(signers),
            "total_weight": total_weight,
            "signers": signers,
            "signer_user_ids": [item["user_id"] for item in signers],
            "signer_addresses": [item["wallet_address"] for item in signers],
            "signature_required_after_governance": True,
            "server_hot_attestation_allowed": True,
            "signer_identity_model": "manager_identity_distinct_from_treasury_signer_wallet_v1",
            "signer_weight_model": "weighted_threshold_v1",
            "device_binding_model": "device_id_and_pubkey_fingerprint_snapshot_v1",
        }

    def _governance_execution_payload_hash_for_row(self, row):
        return self._governance_execution_payload_hash(
            action_type=row["action_type"],
            governance_domain=row["governance_domain"],
            target_wallet_address=row["target_wallet_address"],
            target_address=row["target_address"],
            target_branch=row["target_branch"],
            requested_amount=row["requested_amount"],
            requested_asset=row["requested_asset"],
            payload=_json_loads(row["payload_json"], {}),
        )

    def _governance_execution_guard_error(self, row, payload=None):
        payload = payload if isinstance(payload, dict) else _json_loads(row["payload_json"], {})
        guard = payload.get("execution_guard") if isinstance(payload, dict) else None
        if not isinstance(guard, dict):
            return ""
        expected = {
            "voting_starts_at": row["voting_starts_at"],
            "voting_ends_at": row["voting_ends_at"],
            "timelock_until": row["timelock_until"],
            "timelock_ends_at": row["timelock_ends_at"],
            "expires_at": row["expires_at"],
            "eligible_voter_count": int(row["eligible_voter_count"] or 0),
            "quorum_count": int(row["quorum_count"] or 0),
            GOV_QUORUM_RATE_FIELD: int(row[GOV_QUORUM_RATE_FIELD] or 0),
            GOV_PASS_THRESHOLD_RATE_FIELD: int(row[GOV_PASS_THRESHOLD_RATE_FIELD] or 0),
            GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD: int(row[GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD] or 0),
        }
        for key, expected_value in expected.items():
            actual = guard.get(key)
            if isinstance(expected_value, int):
                try:
                    actual_value = int(actual or 0)
                except Exception:
                    return f"governance execution guard mismatch: {key}"
                if actual_value != expected_value:
                    return f"governance execution guard mismatch: {key}"
            elif str(actual or "") != str(expected_value or ""):
                return f"governance execution guard mismatch: {key}"
        return ""

    def _update_governance_deadline_guard_locked(self, conn, proposal_uuid, *, voting_ends_at=None, expires_at=None):
        row = conn.execute(
            "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
            (str(proposal_uuid or ""),),
        ).fetchone()
        if not row:
            return None
        payload = _json_loads(row["payload_json"], {})
        guard = payload.get("execution_guard") if isinstance(payload, dict) else None
        if isinstance(guard, dict):
            if voting_ends_at is not None:
                guard["voting_ends_at"] = voting_ends_at
            if expires_at is not None:
                guard["expires_at"] = expires_at
        next_voting_ends_at = row["voting_ends_at"] if voting_ends_at is None else voting_ends_at
        next_expires_at = row["expires_at"] if expires_at is None else expires_at
        execution_payload_hash = self._governance_execution_payload_hash(
            action_type=row["action_type"],
            governance_domain=row["governance_domain"],
            target_wallet_address=row["target_wallet_address"],
            target_address=row["target_address"],
            target_branch=row["target_branch"],
            requested_amount=row["requested_amount"],
            requested_asset=row["requested_asset"],
            payload=payload,
        )
        conn.execute(
            """
            UPDATE points_chain_governance_proposals
            SET voting_ends_at=?, expires_at=?, payload_json=?, execution_payload_hash=?, updated_at=?
            WHERE proposal_uuid=? AND status='voting'
            """,
            (
                next_voting_ends_at,
                next_expires_at,
                _json_dumps(payload),
                execution_payload_hash,
                utc_now(),
                row["proposal_uuid"],
            ),
        )
        return conn.execute(
            "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
            (row["proposal_uuid"],),
        ).fetchone()

    def _last_governance_audit_hash(self, conn):
        row = conn.execute("SELECT audit_hash FROM points_chain_governance_audit_log ORDER BY id DESC LIMIT 1").fetchone()
        return row["audit_hash"] if row else ""

    def _append_governance_audit_locked(self, conn, *, proposal_uuid, event_type, actor=None, metadata=None, payload_hash=""):
        now = utc_now()
        prev_hash = self._last_governance_audit_hash(conn)
        audit_uuid = f"pcgovaudit:{uuid.uuid4()}"
        row_payload = {
            "audit_uuid": audit_uuid,
            "proposal_uuid": str(proposal_uuid or ""),
            "event_type": str(event_type or ""),
            "actor_user_id": int(actor_value(actor, "id") or 0) or None,
            "actor_role": self._governance_actor_role(actor) if actor else "",
            "payload_hash": str(payload_hash or ""),
            "metadata": metadata or {},
            "prev_audit_hash": prev_hash,
            "created_at": now,
        }
        audit_hash = sha256_text(canonical_json(row_payload))
        conn.execute(
            """
            INSERT INTO points_chain_governance_audit_log (
                audit_uuid, proposal_uuid, event_type, actor_user_id, actor_role,
                payload_hash, metadata_json, prev_audit_hash, audit_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_uuid,
                row_payload["proposal_uuid"],
                row_payload["event_type"],
                row_payload["actor_user_id"],
                row_payload["actor_role"],
                row_payload["payload_hash"],
                _json_dumps(metadata or {}),
                prev_hash,
                audit_hash,
                now,
            ),
        )
        return {"audit_uuid": audit_uuid, "audit_hash": audit_hash, "prev_audit_hash": prev_hash, "created_at": now}

    def _create_governance_proposal_locked(
        self,
        conn,
        *,
        actor,
        proposal_type,
        governance_domain=None,
        action_type=None,
        title,
        reason,
        description="",
        reference="",
        target_wallet_address="",
        target_address="",
        target_branch="",
        requested_amount=0,
        requested_asset="points",
        incident_tx_hash="",
        base_block_number=None,
        base_block_hash="",
        payload=None,
        evidence=None,
        impact_scope="",
        risk_summary="",
        opposition_record="",
        proposal_severity="NORMAL",
    ):
        action_type = str(action_type or "").strip().upper() or {
            "scam_address_label": "MARK_SCAM",
            "freeze_wallet_address": "FREEZE_ADDRESS",
            "unfreeze_wallet_address": "UNFREEZE_ADDRESS",
            "emergency_recovery_branch": "ROLLBACK_BRANCH",
        }.get(str(proposal_type or ""), "MARK_SCAM")
        if action_type not in GOVERNANCE_ACTION_TYPES:
            raise ValueError("unsupported governance action type")
        governance_domain = self._governance_domain_for_action(action_type, governance_domain or "PUBLIC_COMMON_INTEREST")
        if governance_domain not in GOVERNANCE_DOMAINS:
            raise ValueError("unsupported governance domain")
        self._governance_assert_clock_safe_locked(conn, f"create_governance_proposal:{action_type}")
        self._governance_require_proposer(actor, governance_domain)
        proposal_type = self._governance_legacy_type_for_action(action_type, conn=conn)
        title = str(title or "").strip()[:160]
        description = str(description or "").strip()[:4000]
        reason = str(reason or "").strip()
        reference = str(reference or "").strip()[:240]
        if not title:
            raise ValueError("proposal title required")
        if len(reason) < 8:
            raise ValueError("proposal reason must be at least 8 characters")
        target_wallet_address = str(target_wallet_address or target_address or "").strip().lower()
        target_address = str(target_address or target_wallet_address or "").strip().lower()
        if target_wallet_address:
            if not WALLET_ADDRESS_RE.fullmatch(target_wallet_address):
                raise ValueError("wallet address format is invalid")
        if target_address and target_address.startswith("pc1") and not WALLET_ADDRESS_RE.fullmatch(target_address):
            raise ValueError("target address format is invalid")
        target_branch = str(target_branch or "").strip()[:160]
        requested_amount = int(requested_amount or 0)
        if requested_amount < 0:
            raise ValueError("requested_amount must be >= 0")
        requested_asset = public_currency_text(str(requested_asset or "points").strip().lower())[:40] or "points"
        if incident_tx_hash:
            incident_tx_hash = str(incident_tx_hash).strip()
        payload = dict(payload or {})
        proposal_severity = str(proposal_severity or "NORMAL").strip().upper()
        severity_policy = self._governance_severity_policy(proposal_severity)
        authority = self._governance_proposer_authority(
            conn,
            actor=actor,
            governance_domain=governance_domain,
            action_type=action_type,
            severity=proposal_severity,
            target_address=target_address,
        )
        policy = self._governance_policy(proposal_type, governance_domain=governance_domain, action_type=action_type)
        policy = self._governance_policy_for_payload(policy, action_type=action_type, payload=payload)
        policy = {
            **policy,
            GOV_QUORUM_RATE_FIELD: max(1, int(policy[GOV_QUORUM_RATE_FIELD]) + int(severity_policy[GOV_QUORUM_DELTA_RATE_FIELD])),
            "timelock_seconds": int(policy["timelock_seconds"]) * int(severity_policy["timelock_multiplier"]),
        }
        eligible_voters = self._active_governance_voter_ids(conn, voter_scope=policy["voter_scope"])
        eligible_count = len(eligible_voters)
        if eligible_count < 2:
            raise ValueError("governance proposal requires at least 2 eligible voters")
        quorum_count = self._governance_quorum_count(eligible_count, policy)
        now = utc_now()
        proposal_uuid = f"pcgov:{uuid.uuid4()}"
        timelock_until = self._governance_time_after(policy["timelock_seconds"])
        expires_at = self._governance_time_after(policy["expires_seconds"])
        lifecycle_status = "REVIEW" if authority["sponsor_required"] else "VOTING"
        status = "voting"
        payload["execution_guard"] = {
            "guard_model": "governance_deadline_snapshot_v1",
            "voting_starts_at": now,
            "voting_ends_at": expires_at,
            "timelock_until": timelock_until,
            "timelock_ends_at": timelock_until,
            "expires_at": expires_at,
            "eligible_voter_count": eligible_count,
            "quorum_count": quorum_count,
            GOV_QUORUM_RATE_FIELD: int(policy[GOV_QUORUM_RATE_FIELD]),
            GOV_PASS_THRESHOLD_RATE_FIELD: int(policy[GOV_PASS_THRESHOLD_RATE_FIELD]),
            GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD: int(policy[GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD]),
        }
        execution_payload_hash = self._governance_execution_payload_hash(
            action_type=action_type,
            governance_domain=governance_domain,
            target_wallet_address=target_wallet_address,
            target_address=target_address,
            target_branch=target_branch,
            requested_amount=requested_amount,
            requested_asset=requested_asset,
            payload=payload,
        )
        audit = self._append_governance_audit_locked(
            conn,
            proposal_uuid=proposal_uuid,
            event_type="PROPOSAL_CREATED",
            actor=actor,
            payload_hash=execution_payload_hash,
            metadata={
                "governance_domain": governance_domain,
                "action_type": action_type,
                "target_wallet_address": target_wallet_address,
                "requested_amount": requested_amount,
                "requested_asset": requested_asset,
                "quorum_count": quorum_count,
                "eligible_voter_count": eligible_count,
                "proposal_severity": proposal_severity,
                "sponsor_required": authority["sponsor_required"],
                "proposal_authority": authority["authority"],
            },
        )
        conn.execute(
            f"""
            INSERT INTO points_chain_governance_proposals (
                proposal_uuid, proposal_type, governance_domain, action_type, lifecycle_status,
                proposal_severity, sponsor_required, proposal_deposit_points,
                proposal_deposit_status, title, description, reason, reference, target_wallet_address, target_address,
                target_branch, requested_amount, requested_asset, incident_tx_hash,
                base_block_number, base_block_hash, payload_json, evidence_json,
                impact_scope, risk_summary, opposition_record, eligible_voters_json,
                eligible_voter_count, quorum_count, {GOV_QUORUM_RATE_FIELD}, {GOV_PASS_THRESHOLD_RATE_FIELD},
                {GOV_YES_THRESHOLD_RATE_FIELD}, {GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD}, status,
                voting_starts_at, voting_ends_at, timelock_until, timelock_ends_at,
                expires_at, root_veto_allowed, execution_payload_hash,
                proposer_user_id, proposer_role, prev_audit_hash, audit_hash,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_uuid,
                proposal_type,
                governance_domain,
                action_type,
                lifecycle_status,
                proposal_severity,
                1 if authority["sponsor_required"] else 0,
                int(authority["deposit_points"]),
                authority["deposit_status"],
                title,
                description,
                reason[:2000],
                reference,
                target_wallet_address,
                target_address,
                target_branch,
                requested_amount,
                requested_asset,
                incident_tx_hash,
                int(base_block_number) if base_block_number not in (None, "") else None,
                str(base_block_hash or "").strip()[:128],
                _json_dumps(payload),
                _json_dumps(self._governance_evidence(evidence)),
                str(impact_scope or "").strip()[:1000],
                str(risk_summary or "").strip()[:1000],
                str(opposition_record or "").strip()[:2000],
                _json_dumps(eligible_voters),
                eligible_count,
                quorum_count,
                int(policy[GOV_QUORUM_RATE_FIELD]),
                int(policy[GOV_PASS_THRESHOLD_RATE_FIELD]),
                int(policy[GOV_PASS_THRESHOLD_RATE_FIELD]),
                int(policy[GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD]),
                status,
                now,
                expires_at,
                timelock_until,
                timelock_until,
                expires_at,
                1 if policy["root_veto_allowed"] else 0,
                execution_payload_hash,
                int(actor_value(actor, "id")) if actor_value(actor, "id") else None,
                self._governance_actor_role(actor),
                audit["prev_audit_hash"],
                audit["audit_hash"],
                now,
                now,
            ),
        )
        self._audit_log(
            conn,
            "POINTS_CHAIN_GOVERNANCE_PROPOSAL_CREATED",
            "warning" if proposal_type == "emergency_recovery_branch" else "info",
            f"PointsChain governance proposal created: {proposal_type}",
            actor=actor,
            metadata={
                "proposal_uuid": proposal_uuid,
                "proposal_type": proposal_type,
                "target_wallet_address": target_wallet_address,
                "incident_tx_hash": incident_tx_hash,
                "eligible_voter_count": eligible_count,
                "quorum_count": quorum_count,
                "mode": self._governance_mode(),
            },
        )
        return self._serialize_governance_proposal(conn, conn.execute(
            "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
            (proposal_uuid,),
        ).fetchone(), actor=actor)

    def create_address_risk_proposal(self, *, actor, wallet_address, reason, evidence=None, reference=""):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            proposal = self._create_governance_proposal_locked(
                conn,
                actor=actor,
                proposal_type="scam_address_label",
                governance_domain="PUBLIC_COMMON_INTEREST",
                action_type="MARK_SCAM",
                title="標記疑似詐騙地址",
                description="公開標記高風險或詐騙地址；root 沒有 veto，需全站投票通過。",
                reason=reason,
                reference=reference,
                target_wallet_address=wallet_address,
                target_address=wallet_address,
                evidence=evidence,
                payload={"risk_level": "confirmed_scam", "label": "governance_confirmed_scam"},
                impact_scope="public explorer warning and wallet risk label",
                risk_summary="False positives can damage legitimate wallet reputation; evidence refs are required.",
            )
            conn.commit()
            return {"ok": True, "proposal": proposal}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_wallet_freeze_proposal(self, *, actor, wallet_address, reason, evidence=None, reference="", release=False):
        proposal_type = "unfreeze_wallet_address" if release else "freeze_wallet_address"
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            proposal = self._create_governance_proposal_locked(
                conn,
                actor=actor,
                proposal_type=proposal_type,
                governance_domain="PUBLIC_COMMON_INTEREST",
                action_type="UNFREEZE_ADDRESS" if release else "FREEZE_ADDRESS",
                title="解除治理凍結地址" if release else "治理凍結地址",
                description="公開決定是否限制地址轉出；不改寫 ledger，不沒收餘額。",
                reason=reason,
                reference=reference,
                target_wallet_address=wallet_address,
                target_address=wallet_address,
                evidence=evidence,
                payload={
                    "freeze_model": "block_outgoing_transfers_only",
                    "ledger_mutation": "forbidden",
                    "release": bool(release),
                },
                impact_scope="wallet outgoing transfer gate only",
                risk_summary="A freeze blocks outgoing transfers but does not transfer ownership or mutate confirmed ledger history.",
            )
            conn.commit()
            return {"ok": True, "proposal": proposal}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_emergency_recovery_branch_proposal(
        self,
        *,
        actor,
        incident_tx_hash,
        reason,
        base_block_number=None,
        base_block_hash="",
        excluded_tx_hashes=None,
        recovery_strategy="treasury_compensation",
        loss_cause="protocol_fault",
        compensation_rate_per_10000=None,
        victim_statement="",
        victim_evidence_refs=None,
        victim_claims=None,
        incident_tx_hashes=None,
        product_exposure=None,
        counterparty_reply=None,
        reference="",
    ):
        if not str(incident_tx_hash or "").strip():
            raise ValueError("incident tx hash required")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            strategy = str(recovery_strategy or "treasury_compensation").strip().lower()
            if strategy not in {"treasury_compensation", "tainted_remainder_return", "exclude_tainted_descendants"}:
                raise ValueError("unsupported recovery strategy")
            compensation_policy = self._recovery_compensation_policy(
                loss_cause=loss_cause,
                requested_rate_per_10000=compensation_rate_per_10000,
            )
            reviewed_claims = self._normalize_recovery_claims(
                victim_claims=victim_claims,
                default_statement=victim_statement,
                default_evidence_refs=victim_evidence_refs,
            )
            incident_refs = self._governance_evidence([incident_tx_hash, *(incident_tx_hashes or [])])
            proposal = self._create_governance_proposal_locked(
                conn,
                actor=actor,
                proposal_type="emergency_recovery_branch",
                governance_domain="EMERGENCY_SECURITY",
                action_type="ROLLBACK_BRANCH",
                title="建立緊急 recovery branch",
                description="緊急安全治理決定是否建立 recovery branch；本系統只切換 canonical branch pointer，不刪改舊 ledger。",
                reason=reason,
                reference=reference,
                incident_tx_hash=incident_tx_hash,
                base_block_number=base_block_number,
                base_block_hash=base_block_hash,
                target_branch=f"recovery:{incident_tx_hash}",
                evidence=[incident_tx_hash],
                payload={
                    "incident_tx_hashes": incident_refs,
                    "excluded_tx_hashes": self._governance_evidence(excluded_tx_hashes or []),
                    "recovery_strategy": strategy,
                    "recovery_options": {
                        "treasury_compensation": {
                            "label": "官方補缺",
                            "description": "保留後續交易；排除事故交易後造成的缺口由官方 treasury 依補償政策承擔。",
                            "treasury_compensation": True,
                            "downstream_clawback": False,
                        },
                        "tainted_remainder_return": {
                            "label": "用戶自負責",
                            "description": "保留後續交易；不由官方補償，只把駭客錢包尚未花出的 tainted remainder 退回被盜者。",
                            "treasury_compensation": False,
                            "downstream_clawback": False,
                        },
                        "exclude_tainted_descendants": {
                            "label": "嚴格排除污染後代",
                            "description": "建立 recovery branch 時排除事故交易與可追蹤的污染後代；適用系統性污染，會影響後續收款者。",
                            "treasury_compensation": False,
                            "downstream_clawback": True,
                        },
                    },
                    "loss_cause": compensation_policy["loss_cause"],
                    "compensation_rate_per_10000": compensation_policy["compensation_rate_per_10000"],
                    "compensation_policy": compensation_policy,
                    "victim_statement": str(victim_statement or "").strip()[:4000],
                    "victim_evidence_refs": self._governance_evidence(victim_evidence_refs or []),
                    "victim_claims": reviewed_claims,
                    "counterparty_reply": counterparty_reply if isinstance(counterparty_reply, dict) else {},
                    "product_exposure": product_exposure if isinstance(product_exposure, dict) else {},
                    "branch_model": "branch_isolated_asset_universe_v1",
                    "ledger_mutation": "forbidden",
                    "cross_branch_merge_forbidden": True,
                    "normal_successor_transactions_preserved": strategy in {"treasury_compensation", "tainted_remainder_return"},
                    "victim_shortfall_compensated_by_treasury": strategy == "treasury_compensation",
                    "tainted_remainder_return_only": strategy == "tainted_remainder_return",
                },
                impact_scope="emergency chain branch acceptance; public after-action review required when user balances or trust are affected",
                risk_summary="Recovery branch is a critical emergency action. It creates a new canonical asset universe from parent replay and never mutates historical ledger rows.",
                proposal_severity="CRITICAL",
            )
            conn.commit()
            return {"ok": True, "proposal": proposal}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_treasury_transfer_proposal(
        self,
        *,
        actor,
        destination_wallet_address,
        amount,
        reason,
        reference="",
        action_type="TREASURY_TRANSFER",
        memo="",
    ):
        action_type = str(action_type or "TREASURY_TRANSFER").strip().upper()
        if action_type not in {"TREASURY_TRANSFER", "EXCHANGE_FUND_REPLENISH", "CONTEST_REWARD_PAYOUT"}:
            raise ValueError("unsupported treasury governance action")
        amount = int(amount or 0)
        if amount <= 0:
            raise ValueError("amount must be positive")
        destination = str(destination_wallet_address or "").strip().lower()
        if action_type == "EXCHANGE_FUND_REPLENISH":
            exchange_address = economy_fund_address(self.chain_secret, "exchange_fund")
            if destination and destination != exchange_address:
                raise ValueError("exchange fund replenishment destination must be the EXCHANGE fund system address")
            destination = exchange_address
        if not WALLET_ADDRESS_RE.fullmatch(destination):
            raise ValueError("destination wallet address format is invalid")
        title_map = {
            "TREASURY_TRANSFER": "官方 Treasury 撥款提案",
            "EXCHANGE_FUND_REPLENISH": "撥補交易所基金提案",
            "CONTEST_REWARD_PAYOUT": "競賽獎金撥款提案",
        }
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            destination_fund_key = self._official_transfer_destination_fund_key(destination)
            multisig_policy = self._governance_multisig_policy_locked(conn, fund_key="official_treasury")
            active_branch = self._canonical_branch_uuid(conn)
            proposal = self._create_governance_proposal_locked(
                conn,
                actor=actor,
                proposal_type="official_treasury_operation",
                governance_domain="OFFICIAL_TREASURY",
                action_type=action_type,
                title=title_map[action_type],
                description="官方錢包資產操作由 manager+ 共同投票；root 只能否決，不能單人通過。",
                reason=reason,
                reference=reference,
                target_wallet_address=destination,
                target_address=destination,
                requested_amount=amount,
                requested_asset="points",
                payload={
                    "chain_branch": active_branch,
                    "destination_wallet_address": destination,
                    "destination_fund_key": destination_fund_key or "",
                    "memo": public_currency_text(memo or reason or title_map[action_type]),
                    "official_multisig_policy": multisig_policy,
                },
                evidence=[reference] if reference else [],
                impact_scope="official treasury balance and recipient pending transfer",
                risk_summary="Uses official treasury funds; manager+ approval is required and root may veto before execution.",
            )
            conn.commit()
            return {"ok": True, "proposal": proposal}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_mint_request_proposal(self, *, actor, amount, reason, destination_fund_key="official_treasury", reference=""):
        amount = int(amount or 0)
        if amount <= 0:
            raise ValueError("amount must be positive")
        destination_fund_key = str(destination_fund_key or "official_treasury").strip().lower()
        if destination_fund_key not in {"official_treasury", "promo_fund", "exchange_fund"}:
            raise ValueError("mint destination fund is unsupported")
        reference = str(reference or "").strip()[:240]
        if len(reference) < 8:
            raise ValueError("mint reference/idempotency key required")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            active_branch = self._canonical_branch_uuid(conn)
            policy = bootstrap_economy_layer(
                conn,
                chain_secret=self.chain_secret,
                actor={"role": "system", "id": None},
                chain_branch=active_branch,
            )["policy"]
            self._mint_request_precheck_locked(
                conn,
                amount=amount,
                reference=reference,
                policy=policy,
                chain_branch=active_branch,
                destination_fund_key=destination_fund_key,
            )
            destination = economy_fund_address(self.chain_secret, destination_fund_key)
            multisig_policy = self._governance_multisig_policy_locked(conn, fund_key=destination_fund_key)
            proposal = self._create_governance_proposal_locked(
                conn,
                actor=actor,
                proposal_type="official_treasury_operation",
                governance_domain="OFFICIAL_TREASURY",
                action_type="MINT_REQUEST",
                title="Mint 申請提案",
                description="Mint 只能由 manager+ 通過並經 timelock；root 可 veto，但不能單人 mint。",
                reason=reason,
                reference=reference,
                target_wallet_address=destination,
                target_address=destination,
                requested_amount=amount,
                requested_asset="points",
                payload={
                    "chain_branch": active_branch,
                    "destination_fund_key": destination_fund_key,
                    "official_multisig_policy": multisig_policy,
                },
                impact_scope="max supply, releasable supply, and destination official fund",
                risk_summary="Mint increases active supply and remains capped by max_supply minus reserved_locked.",
            )
            conn.commit()
            return {"ok": True, "proposal": proposal}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _supply_expansion_original_max_supply(self, policy):
        return int(
            (policy or {}).get("constitutional_original_max_supply")
            or (policy or {}).get("original_max_supply")
            or 100_000_000
        )

    def _supply_expansion_restriction(self, policy):
        latest = (policy or {}).get("latest_supply_expansion")
        if isinstance(latest, dict):
            return latest
        restrictions = (policy or {}).get("supply_expansion_restrictions")
        if isinstance(restrictions, list) and restrictions:
            latest = restrictions[-1]
            return latest if isinstance(latest, dict) else {}
        return {}

    def _supply_expansion_mint_portion(self, *, policy, replay, amount):
        original_max = self._supply_expansion_original_max_supply(policy)
        original_releasable = original_max - int((policy or {}).get("reserved_locked") or 0)
        minted_total = int((replay or {}).get("minted_total") or 0)
        before = max(0, minted_total - original_releasable)
        after = max(0, minted_total + int(amount or 0) - original_releasable)
        return max(0, after - before)

    def _supply_expansion_executed_delta_this_year_locked(self, conn, *, year):
        prefix = str(year or utc_now()[:4])[:4]
        total = 0
        rows = conn.execute(
            """
            SELECT payload_json, execution_result_json
            FROM points_chain_governance_proposals
            WHERE action_type='PARAMETER_CHANGE'
              AND status='executed'
              AND executed_at LIKE ?
            """,
            (f"{prefix}%",),
        ).fetchall()
        for row in rows:
            payload = _json_loads(row["payload_json"], {})
            result = _json_loads(row["execution_result_json"], {})
            if str(payload.get("execution_class") or "").strip().upper() != "MONETARY_POLICY_AMENDMENT":
                continue
            if str(payload.get("proposal_type") or "").strip().upper() != "SUPPLY_EXPANSION_REQUEST":
                continue
            total += int(result.get("requested_delta") or payload.get("requested_delta") or 0)
        return total

    def _supply_expansion_eligibility_locked(self, conn, *, requested_delta, destination_fund_key, policy=None, chain_branch=None):
        requested_delta = int(requested_delta or 0)
        if requested_delta <= 0:
            raise ValueError("requested supply expansion delta must be positive")
        destination_fund_key = str(destination_fund_key or "").strip().lower()
        if destination_fund_key not in {"official_treasury", "promo_fund", "exchange_fund"}:
            raise ValueError("supply expansion destination fund is unsupported")
        branch = str(chain_branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        policy = policy or bootstrap_economy_layer(
            conn,
            chain_secret=self.chain_secret,
            actor={"role": "system", "id": None},
            chain_branch=branch,
        )["policy"]
        replay = replay_economy_events(
            conn,
            policy=policy,
            chain_secret=self.chain_secret,
            persist_cache=False,
            chain_branch=branch,
        )
        original_max_supply = self._supply_expansion_original_max_supply(policy)
        max_supply = int(policy["max_supply"])
        single_cap = max(1, original_max_supply // 100)
        annual_cap = max(1, (original_max_supply * 3) // 100)
        if requested_delta > single_cap:
            raise ValueError("supply expansion exceeds single proposal cap")
        used_this_year = self._supply_expansion_executed_delta_this_year_locked(conn, year=utc_now()[:4])
        if used_this_year + requested_delta > annual_cap:
            raise ValueError("supply expansion exceeds annual cap")
        treasury_minimum = int(policy.get("official_treasury_minimum_operating_reserve") or 1_000_000)
        official_balance = int(replay["balances"].get("official_treasury", {}).get("balance") or 0)
        promo_balance = int(replay["balances"].get("promo_fund", {}).get("balance") or 0)
        exchange_assets = int(replay.get("exchange_total_assets") or 0)
        mint_exhausted = int(replay.get("mint_remaining") or 0) <= 0 or int(replay.get("releasable_remaining") or 0) <= 0
        funds_critical = (
            official_balance < treasury_minimum
            and promo_balance < int(policy.get("promo_critical_watermark") or 0)
            and exchange_assets < int(policy.get("exchange_critical_watermark") or 0)
        )
        if not mint_exhausted:
            raise ValueError("supply expansion requires exhausted mint or releasable supply")
        if official_balance >= treasury_minimum:
            raise ValueError("supply expansion requires official treasury below minimum operating reserve")
        if not funds_critical:
            raise ValueError("supply expansion requires treasury, promo, and exchange funds below critical operating watermarks")
        return {
            "eligible": True,
            "chain_branch": branch,
            "old_max_supply": max_supply,
            "requested_new_max_supply": max_supply + requested_delta,
            "requested_delta": requested_delta,
            GOV_DILUTION_RATE_FIELD: (requested_delta * 10000) // max(1, max_supply),
            "destination_fund_key": destination_fund_key,
            "single_expansion_cap": single_cap,
            "annual_expansion_cap": annual_cap,
            "annual_expansion_used": used_this_year,
            "trigger_condition_snapshot": {
                "mint_remaining": int(replay.get("mint_remaining") or 0),
                "releasable_remaining": int(replay.get("releasable_remaining") or 0),
                "mint_exhausted": mint_exhausted,
            },
            "fund_balance_snapshot": {
                "official_treasury": official_balance,
                "official_treasury_minimum_operating_reserve": treasury_minimum,
                "promo_fund": promo_balance,
                "promo_critical_watermark": int(policy.get("promo_critical_watermark") or 0),
                "exchange_total_assets": exchange_assets,
                "exchange_critical_watermark": int(policy.get("exchange_critical_watermark") or 0),
            },
            "alternative_actions_considered": [
                "treasury_reallocation",
                "expense_reduction",
                "subsidy_pause",
                "fee_parameter_adjustment",
                "compensation_installments",
                "non-expansion treasury governance",
            ],
            "policy": policy,
            "replay": replay,
        }

    def create_supply_expansion_request_proposal(
        self,
        *,
        actor,
        requested_delta,
        reason,
        destination_fund_key="official_treasury",
        reference="",
        financial_report="",
        risk_disclosure="",
    ):
        requested_delta = int(requested_delta or 0)
        reference = str(reference or "").strip()[:240]
        if len(reference) < 8:
            raise ValueError("supply expansion reference/idempotency key required")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            active_branch = self._canonical_branch_uuid(conn)
            policy = bootstrap_economy_layer(
                conn,
                chain_secret=self.chain_secret,
                actor={"role": "system", "id": None},
                chain_branch=active_branch,
            )["policy"]
            eligibility = self._supply_expansion_eligibility_locked(
                conn,
                requested_delta=requested_delta,
                destination_fund_key=destination_fund_key,
                policy=policy,
                chain_branch=active_branch,
            )
            duplicate = conn.execute(
                """
                SELECT proposal_uuid
                FROM points_chain_governance_proposals
                WHERE action_type='PARAMETER_CHANGE'
                  AND reference=?
                  AND lifecycle_status NOT IN ('FAILED', 'CANCELLED', 'VETOED', 'EXPIRED')
                LIMIT 1
                """,
                (reference,),
            ).fetchone()
            if duplicate:
                raise ValueError("supply expansion idempotency key already used")
            payload = {
                "proposal_type": "SUPPLY_EXPANSION_REQUEST",
                "execution_class": "MONETARY_POLICY_AMENDMENT",
                "parameter_key": "max_supply",
                "old_max_supply": eligibility["old_max_supply"],
                "requested_new_max_supply": eligibility["requested_new_max_supply"],
                "requested_delta": eligibility["requested_delta"],
                GOV_DILUTION_RATE_FIELD: eligibility[GOV_DILUTION_RATE_FIELD],
                "destination_fund_key": eligibility["destination_fund_key"],
                "spending_restrictions": {
                    "mint_and_spend_are_separate_steps": True,
                    "allowed_mint_destination_fund_key": eligibility["destination_fund_key"],
                    "direct_user_mint": "forbidden",
                    "requires_followup_mint_request": True,
                },
                "trigger_condition_snapshot": eligibility["trigger_condition_snapshot"],
                "fund_balance_snapshot": eligibility["fund_balance_snapshot"],
                "revenue_snapshot": {},
                "alternative_actions_considered": eligibility["alternative_actions_considered"],
                "financial_report": str(financial_report or "").strip()[:4000],
                "risk_disclosure": str(risk_disclosure or "").strip()[:2000],
                "chain_branch": active_branch,
                "single_expansion_cap": eligibility["single_expansion_cap"],
                "annual_expansion_cap": eligibility["annual_expansion_cap"],
                "annual_expansion_used": eligibility["annual_expansion_used"],
                "root_veto_allowed": False,
            }
            proposal = self._create_governance_proposal_locked(
                conn,
                actor=actor,
                proposal_type="protocol_parameter_change",
                governance_domain="PROTOCOL_PARAMETER",
                action_type="PARAMETER_CHANGE",
                title="憲法級增發條款提案",
                description="SUPPLY_EXPANSION_REQUEST / MONETARY_POLICY_AMENDMENT: 只授權提高 max_supply，不自動 mint 或撥款。",
                reason=reason,
                reference=reference,
                requested_amount=requested_delta,
                requested_asset="points",
                payload=payload,
                impact_scope="max_supply monetary policy amendment; all holders are diluted by the requested delta",
                risk_summary=str(risk_disclosure or "Supply expansion dilutes every holder and requires follow-up mint governance into the restricted official fund.")[:1000],
                proposal_severity="CRITICAL",
            )
            conn.commit()
            return {"ok": True, "proposal": proposal, "eligibility": {key: value for key, value in eligibility.items() if key not in {"policy", "replay"}}}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _execute_supply_expansion_request_locked(self, conn, *, row, payload, proposal_uuid, actor, now):
        if str(payload.get("execution_class") or "").strip().upper() != "MONETARY_POLICY_AMENDMENT":
            raise ValueError("supply expansion execution class required")
        if str(payload.get("proposal_type") or "").strip().upper() != "SUPPLY_EXPANSION_REQUEST":
            raise ValueError("supply expansion proposal type required")
        requested_delta = int(payload.get("requested_delta") or row["requested_amount"] or 0)
        destination_fund_key = str(payload.get("destination_fund_key") or "").strip().lower()
        eligibility = self._supply_expansion_eligibility_locked(
            conn,
            requested_delta=requested_delta,
            destination_fund_key=destination_fund_key,
            chain_branch=payload.get("chain_branch") or self._canonical_branch_uuid(conn),
        )
        if int(payload.get("old_max_supply") or 0) != int(eligibility["old_max_supply"]):
            raise ValueError("supply expansion old max supply snapshot mismatch")
        if int(payload.get("requested_new_max_supply") or 0) != int(eligibility["requested_new_max_supply"]):
            raise ValueError("supply expansion requested max supply mismatch")
        policy_row = conn.execute("SELECT * FROM points_economy_policy WHERE id=1").fetchone()
        if not policy_row:
            load_economy_policy(conn)
            policy_row = conn.execute("SELECT * FROM points_economy_policy WHERE id=1").fetchone()
        policy_payload = _json_loads(policy_row["policy_json"], {}) if policy_row else {}
        original_max = int(
            policy_payload.get("constitutional_original_max_supply")
            or policy_payload.get("original_max_supply")
            or eligibility["old_max_supply"]
        )
        existing_restrictions = policy_payload.get("supply_expansion_restrictions")
        if not isinstance(existing_restrictions, list):
            existing_restrictions = []
        authorization = {
            "proposal_uuid": proposal_uuid,
            "execution_class": "MONETARY_POLICY_AMENDMENT",
            "old_max_supply": eligibility["old_max_supply"],
            "new_max_supply": eligibility["requested_new_max_supply"],
            "requested_delta": requested_delta,
            "destination_fund_key": destination_fund_key,
            "spending_restrictions": payload.get("spending_restrictions") if isinstance(payload.get("spending_restrictions"), dict) else {},
            "executed_at": now,
        }
        policy_payload.update({
            "constitutional_original_max_supply": original_max,
            "max_supply": eligibility["requested_new_max_supply"],
            "releasable_supply": eligibility["requested_new_max_supply"] - int(policy_row["reserved_locked"] or 0),
            "latest_supply_expansion": authorization,
            "supply_expansion_restrictions": [*existing_restrictions, authorization],
        })
        conn.execute(
            """
            UPDATE points_economy_policy
            SET max_supply=?, policy_json=?, updated_at=?
            WHERE id=1
            """,
            (
                eligibility["requested_new_max_supply"],
                _json_dumps(policy_payload),
                now,
            ),
        )
        replay = replay_economy_events(
            conn,
            chain_secret=self.chain_secret,
            persist_cache=True,
            chain_branch=eligibility["chain_branch"],
        )
        return {
            "action": "max_supply_expanded",
            "execution_class": "MONETARY_POLICY_AMENDMENT",
            "proposal_type": "SUPPLY_EXPANSION_REQUEST",
            "old_max_supply": eligibility["old_max_supply"],
            "new_max_supply": eligibility["requested_new_max_supply"],
            "requested_delta": requested_delta,
            GOV_DILUTION_RATE_FIELD: eligibility[GOV_DILUTION_RATE_FIELD],
            "destination_fund_key": destination_fund_key,
            "minted": False,
            "mint_and_spend_are_separate_steps": True,
            "replay": {
                "mint_remaining": replay["mint_remaining"],
                "releasable_remaining": replay["releasable_remaining"],
                "wallet_root_hash": replay["wallet_root_hash"],
            },
        }

    def _mint_request_precheck_locked(self, conn, *, amount, reference, policy=None, chain_branch=None, exclude_proposal_uuid="", destination_fund_key=""):
        amount = int(amount or 0)
        if amount <= 0:
            raise ValueError("amount must be positive")
        reference = str(reference or "").strip()
        if len(reference) < 8:
            raise ValueError("mint reference/idempotency key required")
        duplicate_sql = """
            SELECT proposal_uuid, lifecycle_status, status
            FROM points_chain_governance_proposals
            WHERE action_type='MINT_REQUEST'
              AND reference=?
              AND lifecycle_status NOT IN ('FAILED', 'CANCELLED', 'VETOED', 'EXPIRED')
        """
        duplicate_params = [reference]
        if exclude_proposal_uuid:
            duplicate_sql += " AND proposal_uuid<>?"
            duplicate_params.append(str(exclude_proposal_uuid))
        duplicate_sql += " LIMIT 1"
        duplicate = conn.execute(
            duplicate_sql,
            tuple(duplicate_params),
        ).fetchone()
        if duplicate:
            raise ValueError("mint idempotency key already used")
        branch = str(chain_branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        policy = policy or bootstrap_economy_layer(
            conn,
            chain_secret=self.chain_secret,
            actor={"role": "system", "id": None},
            chain_branch=branch,
        )["policy"]
        max_once = int(policy.get("mint_replenish_max_once") or 0)
        if max_once > 0 and amount > max_once:
            raise ValueError("mint amount exceeds per-proposal cap")
        replay = replay_economy_events(
            conn,
            policy=policy,
            chain_secret=self.chain_secret,
            persist_cache=False,
            chain_branch=branch,
        )
        releasable = int(policy["max_supply"]) - int(policy["reserved_locked"])
        if int(replay["minted_total"]) + amount > releasable:
            if int(replay["minted_total"]) >= releasable:
                raise ValueError("mint_supply_exhausted")
            raise ValueError("mint would exceed releasable supply")
        expanded_portion = self._supply_expansion_mint_portion(policy=policy, replay=replay, amount=amount)
        if expanded_portion > 0:
            restriction = self._supply_expansion_restriction(policy)
            restricted_destination = str(restriction.get("destination_fund_key") or "").strip().lower()
            if not restricted_destination:
                raise ValueError("supply_expansion_authorization_required")
            if str(destination_fund_key or "").strip().lower() != restricted_destination:
                raise ValueError("mint destination violates supply expansion restriction")
        daily_cap = int(policy.get("mint_replenish_daily_cap") or max_once or 0)
        if daily_cap > 0:
            today = utc_now()[:10]
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS total
                FROM points_economy_events
                WHERE status='confirmed'
                  AND chain_branch=?
                  AND source_fund_key='mint'
                  AND transaction_type='governance_mint_request'
                  AND created_at LIKE ?
                """,
                (branch, f"{today}%"),
            ).fetchone()
            if int(row["total"] or 0) + amount > daily_cap:
                raise ValueError("mint amount exceeds daily cap")
        return {"policy": policy, "replay": replay, "daily_cap": daily_cap, "per_proposal_cap": max_once}

    def create_public_governance_proposal(
        self,
        *,
        actor,
        action_type,
        title,
        reason,
        target_address="",
        incident_tx_hash="",
        reference="",
        evidence=None,
        proposal_severity="NORMAL",
        description="",
        impact_scope="",
        risk_summary="",
        payload=None,
    ):
        action_type = str(action_type or "").strip().upper()
        if action_type in {"ROLLBACK_BRANCH", "HARD_FORK_ACCEPTANCE"}:
            raise PermissionError("rollback and hard-fork proposals require manager/security sponsor and cannot be filed through the public proposal endpoint")
        if action_type not in {"MARK_SCAM", "FREEZE_ADDRESS", "UNFREEZE_ADDRESS", "AUTO_BURN_POLICY"}:
            raise ValueError("unsupported public governance action")
        if action_type in {"MARK_SCAM", "FREEZE_ADDRESS", "UNFREEZE_ADDRESS"} and not str(target_address or "").strip():
            raise ValueError("target address required")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            proposal = self._create_governance_proposal_locked(
                conn,
                actor=actor,
                proposal_type=self._governance_legacy_type_for_action(action_type, conn=conn),
                governance_domain="PUBLIC_COMMON_INTEREST",
                action_type=action_type,
                title=title or {
                    "MARK_SCAM": "標記疑似詐騙地址",
                    "FREEZE_ADDRESS": "治理凍結地址",
                    "UNFREEZE_ADDRESS": "解除治理凍結地址",
                    "ROLLBACK_BRANCH": "建立緊急 recovery branch",
                    "AUTO_BURN_POLICY": "自動銷毀政策",
                    "HARD_FORK_ACCEPTANCE": "Hard fork 接受案",
                }.get(action_type, "公共治理提案"),
                description=description,
                reason=reason,
                reference=reference,
                target_wallet_address=target_address,
                target_address=target_address,
                target_branch=f"recovery:{incident_tx_hash}" if incident_tx_hash else "",
                incident_tx_hash=incident_tx_hash,
                evidence=evidence,
                payload=payload or {},
                impact_scope=impact_scope,
                risk_summary=risk_summary,
                proposal_severity=proposal_severity,
            )
            conn.commit()
            return {"ok": True, "proposal": proposal}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_policy_governance_proposal(
        self,
        *,
        actor,
        action_type,
        title,
        reason,
        payload=None,
        reference="",
        proposal_severity="NORMAL",
        impact_scope="",
        risk_summary="",
    ):
        action_type = str(action_type or "").strip().upper()
        if action_type not in {"EMERGENCY_LOCKDOWN", "PARAMETER_CHANGE", "FEATURE_ACTIVATION", "AUTO_BURN_POLICY", "TREASURY_SIGNER_CHANGE"}:
            raise ValueError("unsupported policy governance action")
        domain = "OFFICIAL_TREASURY" if action_type == "TREASURY_SIGNER_CHANGE" else "EMERGENCY_SECURITY" if action_type == "EMERGENCY_LOCKDOWN" else (
            "PUBLIC_COMMON_INTEREST" if action_type == "AUTO_BURN_POLICY" else "PROTOCOL_PARAMETER"
        )
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            proposal = self._create_governance_proposal_locked(
                conn,
                actor=actor,
                proposal_type=self._governance_legacy_type_for_action(action_type, conn=conn),
                governance_domain=domain,
                action_type=action_type,
                title=title or action_type,
                description=str((payload or {}).get("description") or "")[:4000],
                reason=reason,
                reference=reference,
                payload=payload or {},
                impact_scope=impact_scope,
                risk_summary=risk_summary,
                proposal_severity=proposal_severity,
            )
            conn.commit()
            return {"ok": True, "proposal": proposal}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_transaction_dispute_schema(self, conn):
        db_path = _connection_path(conn)
        if db_path and db_path in self._transaction_dispute_schema_ready_paths:
            return
        with self._transaction_dispute_schema_lock:
            if db_path and db_path in self._transaction_dispute_schema_ready_paths:
                return
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS points_chain_transaction_disputes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dispute_uuid TEXT NOT NULL UNIQUE,
                    tx_hash TEXT NOT NULL,
                    reporter_user_id INTEGER NOT NULL,
                    reporter_username TEXT NOT NULL DEFAULT '',
                    victim_wallet_address TEXT NOT NULL DEFAULT '',
                    claimed_amount_points INTEGER NOT NULL DEFAULT 0,
                    loss_cause TEXT NOT NULL DEFAULT 'unknown',
                    statement TEXT NOT NULL,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    reviewed_by INTEGER,
                    reviewed_at TEXT,
                    review_note TEXT NOT NULL DEFAULT '',
                    recommended_strategy TEXT NOT NULL DEFAULT '',
                    governance_proposal_uuid TEXT NOT NULL DEFAULT '',
                    suspect_wallet_address TEXT NOT NULL DEFAULT '',
                    address_risk_proposal_uuid TEXT NOT NULL DEFAULT '',
                    address_freeze_proposal_uuid TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK (status IN ('pending_review', 'approved', 'rejected', 'proposal_created', 'cancelled'))
                )
                """
            )
            cols = table_columns(conn, "points_chain_transaction_disputes")
            additions = {
                "suspect_wallet_address": "TEXT NOT NULL DEFAULT ''",
                "address_risk_proposal_uuid": "TEXT NOT NULL DEFAULT ''",
                "address_freeze_proposal_uuid": "TEXT NOT NULL DEFAULT ''",
                "from_wallet_address": "TEXT NOT NULL DEFAULT ''",
                "to_wallet_address": "TEXT NOT NULL DEFAULT ''",
                "chain_branch": "TEXT NOT NULL DEFAULT 'main'",
                "signature_runtime_mode": "TEXT NOT NULL DEFAULT ''",
                "signature_purpose": "TEXT NOT NULL DEFAULT ''",
                "statement_hash": "TEXT NOT NULL DEFAULT ''",
                "evidence_hash": "TEXT NOT NULL DEFAULT ''",
                "signature_nonce": "TEXT NOT NULL DEFAULT ''",
                "from_signature": "TEXT NOT NULL DEFAULT ''",
                "open_signed_payload_hash": "TEXT NOT NULL DEFAULT ''",
                "open_signature_hash": "TEXT NOT NULL DEFAULT ''",
                "from_public_key_jwk_json": "TEXT NOT NULL DEFAULT '{}'",
                "from_signature_verified": "INTEGER NOT NULL DEFAULT 0",
                "reply_statement": "TEXT NOT NULL DEFAULT ''",
                "reply_evidence_json": "TEXT NOT NULL DEFAULT '[]'",
                "reply_statement_hash": "TEXT NOT NULL DEFAULT ''",
                "reply_evidence_hash": "TEXT NOT NULL DEFAULT ''",
                "reply_nonce": "TEXT NOT NULL DEFAULT ''",
                "reply_signature": "TEXT NOT NULL DEFAULT ''",
                "reply_signed_payload_hash": "TEXT NOT NULL DEFAULT ''",
                "reply_signature_hash": "TEXT NOT NULL DEFAULT ''",
                "reply_public_key_jwk_json": "TEXT NOT NULL DEFAULT '{}'",
                "reply_signature_verified": "INTEGER NOT NULL DEFAULT 0",
                "reply_created_at": "TEXT",
                "initial_freeze_expires_at": "TEXT",
                "escalated_freeze_expires_at": "TEXT",
                "identity_redaction_model": "TEXT NOT NULL DEFAULT 'address_proven_anonymous_v1'",
                "dispute_bond_points": "INTEGER NOT NULL DEFAULT 0",
                "bond_status": "TEXT NOT NULL DEFAULT 'not_collected'",
            }
            for column, ddl in additions.items():
                if column in cols:
                    continue
                try:
                    conn.execute(f"ALTER TABLE points_chain_transaction_disputes ADD COLUMN {column} {ddl}")
                except sqlite3.OperationalError as exc:
                    if not _is_duplicate_column_error(exc):
                        raise
                cols.add(column)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_points_chain_disputes_tx ON points_chain_transaction_disputes(tx_hash, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_points_chain_disputes_reporter ON points_chain_transaction_disputes(reporter_user_id, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_points_chain_disputes_from_address ON points_chain_transaction_disputes(from_wallet_address, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_points_chain_disputes_to_address ON points_chain_transaction_disputes(to_wallet_address, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_points_chain_disputes_open_sig_hash ON points_chain_transaction_disputes(open_signature_hash)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_points_chain_disputes_reply_sig_hash ON points_chain_transaction_disputes(reply_signature_hash)")
            if db_path:
                self._transaction_dispute_schema_ready_paths.add(db_path)

    def _dispute_suspect_address_from_tx(self, tx_hash, victim_wallet_address=""):
        tx = self.explorer_transaction(tx_hash)
        if not tx:
            return ""
        payload = tx.get("transaction") or {}
        flow = payload.get("wallet_flow") if isinstance(payload.get("wallet_flow"), dict) else {}
        source = str(payload.get("source_wallet_address") or flow.get("source_wallet_address") or "").strip().lower()
        destination = str(payload.get("destination_wallet_address") or flow.get("destination_wallet_address") or "").strip().lower()
        victim = str(victim_wallet_address or "").strip().lower()
        for candidate in (destination, source):
            if not WALLET_ADDRESS_RE.fullmatch(candidate):
                continue
            if victim and candidate == victim:
                continue
            if self._explorer_fund_key_for_address(candidate):
                continue
            return candidate
        return ""

    def _address_dispute_details_from_tx(self, tx_hash):
        tx = self.explorer_transaction(tx_hash)
        if not tx:
            raise ValueError("transaction not found")
        payload = tx.get("transaction") or {}
        flow = payload.get("wallet_flow") if isinstance(payload.get("wallet_flow"), dict) else {}
        source = str(payload.get("source_wallet_address") or flow.get("source_wallet_address") or "").strip().lower()
        destination = str(payload.get("destination_wallet_address") or flow.get("destination_wallet_address") or "").strip().lower()
        if not WALLET_ADDRESS_RE.fullmatch(source):
            raise ValueError("disputed transaction source address is not a wallet address")
        if not WALLET_ADDRESS_RE.fullmatch(destination):
            raise ValueError("disputed transaction destination address is not a wallet address")
        amount = int(payload.get("amount_points") if payload.get("amount_points") is not None else payload.get("amount") or 0)
        branch = str(payload.get("chain_branch") or tx.get("chain_branch") or "main").strip() or "main"
        return {
            "tx_hash": str(payload.get("transaction_hash") or payload.get("ledger_hash") or tx_hash).strip(),
            "from_wallet_address": source,
            "to_wallet_address": destination,
            "amount_points": max(0, amount),
            "chain_branch": branch,
            "payload": payload,
        }

    def _address_dispute_statement_hash(self, statement):
        return sha256_text(str(statement or ""))

    def _address_dispute_evidence_hash(self, evidence):
        return sha256_text(canonical_json(self._governance_evidence(evidence or [])))

    def _address_dispute_runtime_mode(self):
        mode = str(self._governance_mode() or "unknown").strip()
        return mode or "unknown"

    def _address_dispute_payload_hash(
        self,
        *,
        tx_hash,
        from_wallet_address,
        to_wallet_address,
        amount_points,
        statement_hash,
        evidence_hash,
        nonce,
        chain_branch,
        purpose,
        runtime_mode,
    ):
        payload = address_dispute_payload(
            tx_hash=tx_hash,
            from_wallet_address=from_wallet_address,
            to_wallet_address=to_wallet_address,
            amount_points=amount_points,
            statement_hash=statement_hash,
            evidence_hash=evidence_hash,
            nonce=nonce,
            chain_branch=chain_branch,
            purpose=purpose,
            runtime_mode=runtime_mode,
        )
        return sha256_text(canonical_json(payload))

    def _assert_address_dispute_signature_not_replayed_locked(self, conn, *, signed_payload_hash, signature_hash, exclude_dispute_uuid=""):
        signed_payload_hash = str(signed_payload_hash or "").strip()
        signature_hash = str(signature_hash or "").strip()
        if not signed_payload_hash or not signature_hash:
            raise ValueError("address dispute signature replay guard is missing")
        params = [signed_payload_hash, signature_hash, signed_payload_hash, signature_hash]
        exclude_clause = ""
        if str(exclude_dispute_uuid or "").strip():
            exclude_clause = " AND dispute_uuid<>?"
            params.append(str(exclude_dispute_uuid or "").strip())
        row = conn.execute(
            f"""
            SELECT dispute_uuid
            FROM points_chain_transaction_disputes
            WHERE (
                open_signed_payload_hash=?
                OR open_signature_hash=?
                OR reply_signed_payload_hash=?
                OR reply_signature_hash=?
            ){exclude_clause}
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()
        if row:
            raise ValueError("address dispute signed payload/signature replay rejected")

    def _assert_dispute_reply_has_no_identity_leak(self, text):
        lowered = str(text or "").lower()
        blocked = [
            "user_id",
            "username",
            "email",
            "nickname",
            "暱稱",
            "用戶名",
            "會員",
            "帳號",
            "@",
        ]
        for token in blocked:
            if token in lowered:
                raise ValueError("address dispute text must not include account identity fields; use address-only evidence")

    def _active_dispute_count_for_from_address_locked(self, conn, from_wallet_address):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        row = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM points_chain_transaction_disputes
            WHERE from_wallet_address=?
              AND created_at>=?
              AND status IN ('pending_review', 'approved', 'proposal_created')
            """,
            (from_wallet_address, cutoff),
        ).fetchone()
        return int(row["c"] or 0) if row else 0

    def _account_bound_official_hot_wallet_for_actor_locked(self, conn, *, actor_id, wallet_address):
        actor_id = int(actor_id or 0)
        wallet_address = str(wallet_address or "").strip().lower()
        if actor_id <= 0 or not WALLET_ADDRESS_RE.fullmatch(wallet_address):
            return None
        return conn.execute(
            """
            SELECT * FROM points_wallet_identities
            WHERE user_id=?
              AND address=?
              AND wallet_type='official_hot'
              AND custody_mode='server_hot'
              AND status IN ('pending_backup', 'active')
            LIMIT 1
            """,
            (actor_id, wallet_address),
        ).fetchone()

    def _official_hot_wallet_owner_for_address_locked(self, conn, wallet_address):
        wallet_address = str(wallet_address or "").strip().lower()
        if not WALLET_ADDRESS_RE.fullmatch(wallet_address):
            return None
        return conn.execute(
            """
            SELECT w.user_id, u.username, w.address
            FROM points_wallet_identities w
            JOIN users u ON u.id=w.user_id
            WHERE w.address=?
              AND w.wallet_type='official_hot'
              AND w.custody_mode='server_hot'
              AND w.status IN ('pending_backup', 'active')
              AND COALESCE(u.status, 'active')='active'
            LIMIT 1
            """,
            (wallet_address,),
        ).fetchone()

    def _active_notification_user_ids_locked(self, conn):
        rows = conn.execute(
            """
            SELECT id FROM users
            WHERE COALESCE(status, 'active')='active'
            ORDER BY id ASC
            """
        ).fetchall()
        return [int(row["id"]) for row in rows if int(row["id"] or 0) > 0]

    def _transaction_dispute_notification_clues(self, *, tx_hash, statement="", evidence_list=None):
        clues = [str(tx_hash or "").strip()]
        if str(statement or "").strip():
            clues.append(f"申訴說明：{str(statement or '').strip()}")
        clues.extend(str(item or "").strip() for item in (evidence_list or []) if str(item or "").strip())
        text = "；".join(item for item in clues if item)
        if len(text) > 280:
            text = text[:277] + "..."
        return text or "未提供額外線索"

    def _notify_transaction_dispute_opened_locked(
        self,
        conn,
        *,
        dispute_uuid,
        tx_hash,
        from_address,
        to_address,
        amount,
        branch,
        evidence_list,
        reply_deadline=None,
        statement="",
    ):
        from services.system.notifications import create_notification

        direct_owner = self._official_hot_wallet_owner_for_address_locked(conn, to_address)
        if direct_owner:
            recipient_ids = [int(direct_owner["user_id"])]
            audience = "user"
            direct_official_hot_owner = True
        else:
            recipient_ids = self._active_notification_user_ids_locked(conn)
            audience = "system"
            direct_official_hot_owner = False
        clues = self._transaction_dispute_notification_clues(tx_hash=tx_hash, statement=statement, evidence_list=evidence_list)
        deadline = str(reply_deadline or "").strip() or "初始短期凍結期限"
        body = (
            f"To 的地址 {to_address} 被懷疑有詐騙行為，相關線索：{clues}。"
            f"請 To 地址擁有者進行回覆，請於 {deadline} 前回覆；若無則逕付治理投票。"
        )
        metadata = {
            "action": "points_chain_dispute_reply",
            "dispute_uuid": str(dispute_uuid or ""),
            "tx_hash": str(tx_hash or ""),
            "from_wallet_address": str(from_address or ""),
            "to_wallet_address": str(to_address or ""),
            "claimed_amount_points": int(amount or 0),
            "chain_branch": str(branch or "main"),
            "reply_deadline": deadline,
            "direct_official_hot_owner": direct_official_hot_owner,
        }
        created_count = 0
        for recipient_id in sorted(set(recipient_ids)):
            if create_notification(
                conn,
                user_id=recipient_id,
                type="points_chain_address_dispute_reply_required",
                title="To 地址疑義回覆請求",
                body=body,
                link="#economy-transactions",
                severity="warning",
                audience=audience,
                source_module="points_chain_dispute",
                source_ref=str(dispute_uuid or ""),
                metadata_json=_json_dumps(metadata),
                expires_at=reply_deadline,
            ):
                created_count += 1
        return {
            "created_count": created_count,
            "direct_official_hot_owner": direct_official_hot_owner,
            "recipient_count": len(set(recipient_ids)),
        }

    def _official_hot_wallet_labels_locked(self, conn, *, actor=None, addresses=None):
        if not self._governance_is_manager_actor(actor):
            return {}
        normalized = sorted({
            str(item or "").strip().lower()
            for item in (addresses or [])
            if WALLET_ADDRESS_RE.fullmatch(str(item or "").strip().lower())
        })
        if not normalized:
            return {}
        placeholders = ", ".join("?" for _ in normalized)
        rows = conn.execute(
            f"""
            SELECT w.address, u.username
            FROM points_wallet_identities w
            JOIN users u ON u.id=w.user_id
            WHERE w.address IN ({placeholders})
              AND w.wallet_type='official_hot'
              AND w.custody_mode='server_hot'
              AND w.status IN ('pending_backup', 'active')
              AND COALESCE(u.status, 'active')='active'
            """,
            tuple(normalized),
        ).fetchall()
        return {
            str(row["address"] or "").strip().lower(): str(row["username"] or "").strip()
            for row in rows
            if str(row["address"] or "").strip()
        }

    def _collect_wallet_addresses_from_value(self, value):
        found = set()
        if isinstance(value, dict):
            for key, item in value.items():
                lowered = str(key or "").lower()
                if lowered.endswith("address") or lowered.endswith("wallet_address"):
                    text = str(item or "").strip().lower()
                    if WALLET_ADDRESS_RE.fullmatch(text):
                        found.add(text)
                found.update(self._collect_wallet_addresses_from_value(item))
        elif isinstance(value, list):
            for item in value:
                found.update(self._collect_wallet_addresses_from_value(item))
        elif isinstance(value, str):
            text = value.strip().lower()
            if WALLET_ADDRESS_RE.fullmatch(text):
                found.add(text)
        return found

    def create_transaction_dispute(
        self,
        *,
        actor,
        tx_hash,
        statement,
        victim_wallet_address="",
        claimed_amount_points=0,
        loss_cause="unknown",
        evidence=None,
        public_key_jwk=None,
        signature="",
        signature_nonce="",
        from_wallet_address="",
        to_wallet_address="",
        chain_branch="",
        account_bound_proof=False,
    ):
        user_id = int(actor_value(actor, "id") or 0)
        if user_id <= 0:
            raise PermissionError("login required")
        if str(actor_value(actor, "username") or "") == "root":
            raise PermissionError("root 帳號不使用匿名地址疑義流程；官方錢包或官方地址事故請改走官方治理、內部帳務治理或緊急安全治理。")
        tx_hash = str(tx_hash or "").strip()
        if not tx_hash:
            raise ValueError("transaction hash required")
        statement = str(statement or "").strip()
        if len(statement) < 12:
            raise ValueError("statement must be at least 12 characters")
        self._assert_dispute_reply_has_no_identity_leak(statement)
        evidence_list = self._governance_evidence(evidence or [])
        for item in evidence_list:
            self._assert_dispute_reply_has_no_identity_leak(str(item or ""))
        details = self._address_dispute_details_from_tx(tx_hash)
        from_address = str(from_wallet_address or details["from_wallet_address"]).strip().lower()
        to_address = str(to_wallet_address or details["to_wallet_address"]).strip().lower()
        if from_address != details["from_wallet_address"]:
            raise ValueError("dispute from address must match transaction source")
        if to_address != details["to_wallet_address"]:
            raise ValueError("dispute to address must match transaction destination")
        branch = str(chain_branch or details["chain_branch"] or "main").strip() or "main"
        if branch != details["chain_branch"]:
            raise ValueError("dispute chain branch must match transaction branch")
        amount = max(0, int(claimed_amount_points or details["amount_points"] or 0))
        if amount != int(details["amount_points"] or 0):
            raise ValueError("dispute amount must match transaction amount")
        statement_hash = self._address_dispute_statement_hash(statement)
        evidence_hash = self._address_dispute_evidence_hash(evidence_list)
        signature_nonce = str(signature_nonce or "").strip()
        runtime_mode = self._address_dispute_runtime_mode()
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._ensure_transaction_dispute_schema(conn)
            proof_model = "address_proven_anonymous_v1"
            signature_purpose = "address_dispute_open"
            signature_value = str(signature or "")
            canonical_jwk = {}
            signed_proof = bool(public_key_jwk and signature and signature_nonce)
            if signed_proof:
                verify_wallet_address_dispute_signature(
                    tx_hash=tx_hash,
                    from_wallet_address=from_address,
                    to_wallet_address=to_address,
                    amount_points=amount,
                    statement_hash=statement_hash,
                    evidence_hash=evidence_hash,
                    nonce=signature_nonce,
                    chain_branch=branch,
                    purpose="address_dispute_open",
                    signer_wallet_address=from_address,
                    public_key_jwk=public_key_jwk,
                    signature=signature,
                    runtime_mode=runtime_mode,
                )
                open_signed_payload_hash = self._address_dispute_payload_hash(
                    tx_hash=tx_hash,
                    from_wallet_address=from_address,
                    to_wallet_address=to_address,
                    amount_points=amount,
                    statement_hash=statement_hash,
                    evidence_hash=evidence_hash,
                    nonce=signature_nonce,
                    chain_branch=branch,
                    purpose="address_dispute_open",
                    runtime_mode=runtime_mode,
                )
                open_signature_hash = sha256_text(str(signature or "").strip())
                canonical_jwk = canonical_public_jwk(public_key_jwk)
            else:
                official_hot_wallet = self._account_bound_official_hot_wallet_for_actor_locked(
                    conn,
                    actor_id=user_id,
                    wallet_address=from_address,
                )
                if not official_hot_wallet:
                    raise ValueError("address-signed dispute proof is required unless From is your account-bound official hot wallet")
                if not signature_nonce:
                    signature_nonce = f"account-bound:{uuid.uuid4()}"
                signature_purpose = "account_bound_dispute_open"
                proof_model = "account_bound_official_hot_v1"
                account_bound_payload = {
                    "amount_points": amount,
                    "chain_branch": branch,
                    "custody_mode": "server_hot",
                    "evidence_hash": evidence_hash,
                    "from_wallet_address": from_address,
                    "nonce": signature_nonce,
                    "purpose": signature_purpose,
                    "runtime_mode": runtime_mode,
                    "statement_hash": statement_hash,
                    "to_wallet_address": to_address,
                    "tx_hash": tx_hash,
                    "wallet_type": "official_hot",
                }
                open_signed_payload_hash = sha256_text(canonical_json(account_bound_payload))
                open_signature_hash = sha256_text(f"account_bound_official_hot_v1:{from_address}:{open_signed_payload_hash}")
                signature_value = "account_bound_official_hot_v1"
            self._assert_address_dispute_signature_not_replayed_locked(
                conn,
                signed_payload_hash=open_signed_payload_hash,
                signature_hash=open_signature_hash,
            )
            duplicate = conn.execute(
                """
                SELECT dispute_uuid FROM points_chain_transaction_disputes
                WHERE tx_hash=? AND status IN ('pending_review', 'approved', 'proposal_created')
                LIMIT 1
                """,
                (tx_hash,),
            ).fetchone()
            if duplicate:
                raise ValueError("active dispute already exists for this transaction")
            if self._active_dispute_count_for_from_address_locked(conn, from_address) >= DISPUTE_FROM_DAILY_LIMIT:
                raise ValueError("address dispute daily limit exceeded")
            now = utc_now()
            dispute_uuid = f"pcdispute:{uuid.uuid4()}"
            initial_freeze = self._create_provisional_address_freeze_locked(
                conn,
                wallet_address=to_address,
                reason=f"one-hour address-proven transaction dispute hold: {dispute_uuid}",
                evidence=[tx_hash, *evidence_list],
                source_dispute_uuid=dispute_uuid,
                actor=None,
                ttl_seconds=DISPUTE_INITIAL_FREEZE_SECONDS,
            )
            conn.execute(
                """
                INSERT INTO points_chain_transaction_disputes (
                    dispute_uuid, tx_hash, reporter_user_id, reporter_username,
                    victim_wallet_address, claimed_amount_points, loss_cause,
                    statement, evidence_json, status, suspect_wallet_address,
                    from_wallet_address, to_wallet_address, chain_branch,
                    signature_runtime_mode, signature_purpose, statement_hash, evidence_hash, signature_nonce,
                    from_signature, open_signed_payload_hash, open_signature_hash,
                    from_public_key_jwk_json, from_signature_verified,
                    initial_freeze_expires_at, identity_redaction_model,
                    created_at, updated_at
                ) VALUES (?, ?, 0, '', ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                """,
                (
                    dispute_uuid,
                    tx_hash,
                    from_address,
                    amount,
                    str(loss_cause or "unknown").strip().lower(),
                    statement[:4000],
                    _json_dumps(evidence_list),
                    to_address,
                    from_address,
                    to_address,
                    branch,
                    runtime_mode,
                    signature_purpose,
                    statement_hash,
                    evidence_hash,
                    signature_nonce[:120],
                    signature_value,
                    open_signed_payload_hash,
                    open_signature_hash,
                    canonical_json(canonical_jwk),
                    initial_freeze.get("expires_at") if initial_freeze else None,
                    proof_model,
                    now,
                    now,
                ),
            )
            self._audit_log(
                conn,
                "POINTS_CHAIN_ADDRESS_DISPUTE_OPENED",
                "warning",
                "Address-proven transaction dispute opened",
                actor=None,
                metadata={
                    "dispute_uuid": dispute_uuid,
                    "tx_hash": tx_hash,
                    "from_wallet_address": from_address,
                    "to_wallet_address": to_address,
                    "chain_branch": branch,
                    "identity_redaction_model": proof_model,
                    "signature_purpose": signature_purpose,
                    "initial_freeze_expires_at": initial_freeze.get("expires_at") if initial_freeze else None,
                },
            )
            notification_status = self._notify_transaction_dispute_opened_locked(
                conn,
                dispute_uuid=dispute_uuid,
                tx_hash=tx_hash,
                from_address=from_address,
                to_address=to_address,
                amount=amount,
                branch=branch,
                evidence_list=evidence_list,
                statement=statement,
                reply_deadline=initial_freeze.get("expires_at") if initial_freeze else None,
            )
            conn.commit()
            return {
                "ok": True,
                "dispute": self._serialize_transaction_dispute(conn, dispute_uuid),
                "initial_provisional_freeze": initial_freeze,
                "notifications": notification_status,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _serialize_transaction_dispute(self, conn, dispute_uuid):
        row = conn.execute("SELECT * FROM points_chain_transaction_disputes WHERE dispute_uuid=?", (dispute_uuid,)).fetchone()
        if not row:
            return None
        keys = set(row.keys())
        return {
            "dispute_uuid": row["dispute_uuid"],
            "tx_hash": row["tx_hash"],
            "victim_wallet_address": row["victim_wallet_address"],
            "from_wallet_address": row["from_wallet_address"] if "from_wallet_address" in keys else row["victim_wallet_address"],
            "to_wallet_address": row["to_wallet_address"] if "to_wallet_address" in keys else (row["suspect_wallet_address"] if "suspect_wallet_address" in keys else ""),
            "chain_branch": row["chain_branch"] if "chain_branch" in keys else "main",
            "claimed_amount_points": int(row["claimed_amount_points"] or 0),
            "loss_cause": row["loss_cause"],
            "statement": row["statement"],
            "evidence": _json_loads(row["evidence_json"], []),
            "status": row["status"],
            "reviewed_by": "governance_operator" if row["reviewed_by"] else None,
            "reviewed_at": row["reviewed_at"],
            "review_note": row["review_note"],
            "recommended_strategy": row["recommended_strategy"],
            "governance_proposal_uuid": row["governance_proposal_uuid"],
            "suspect_wallet_address": row["suspect_wallet_address"] if "suspect_wallet_address" in keys else "",
            "address_risk_proposal_uuid": row["address_risk_proposal_uuid"] if "address_risk_proposal_uuid" in keys else "",
            "address_freeze_proposal_uuid": row["address_freeze_proposal_uuid"] if "address_freeze_proposal_uuid" in keys else "",
            "signature_runtime_mode": row["signature_runtime_mode"] if "signature_runtime_mode" in keys else "",
            "signature_purpose": row["signature_purpose"] if "signature_purpose" in keys else "",
            "statement_hash": row["statement_hash"] if "statement_hash" in keys else "",
            "evidence_hash": row["evidence_hash"] if "evidence_hash" in keys else "",
            "signature_nonce": row["signature_nonce"] if "signature_nonce" in keys else "",
            "from_signature_verified": bool(row["from_signature_verified"]) if "from_signature_verified" in keys else False,
            "reply_statement": row["reply_statement"] if "reply_statement" in keys else "",
            "reply_evidence": _json_loads(row["reply_evidence_json"], []) if "reply_evidence_json" in keys else [],
            "reply_statement_hash": row["reply_statement_hash"] if "reply_statement_hash" in keys else "",
            "reply_evidence_hash": row["reply_evidence_hash"] if "reply_evidence_hash" in keys else "",
            "reply_signature_verified": bool(row["reply_signature_verified"]) if "reply_signature_verified" in keys else False,
            "reply_created_at": row["reply_created_at"] if "reply_created_at" in keys else None,
            "initial_freeze_expires_at": row["initial_freeze_expires_at"] if "initial_freeze_expires_at" in keys else None,
            "escalated_freeze_expires_at": row["escalated_freeze_expires_at"] if "escalated_freeze_expires_at" in keys else None,
            "identity_redaction_model": row["identity_redaction_model"] if "identity_redaction_model" in keys else "legacy_user_bound",
            "dispute_bond_points": int(row["dispute_bond_points"] or 0) if "dispute_bond_points" in keys else 0,
            "bond_status": row["bond_status"] if "bond_status" in keys else "not_collected",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _dispute_reply_payload_from_row(self, row, *, wallet_address=""):
        keys = set(row.keys())
        if "reply_statement" not in keys or not str(row["reply_statement"] or "").strip():
            return {}
        return {
            "wallet_address": str(wallet_address or row["to_wallet_address"] or row["suspect_wallet_address"] or "").strip().lower(),
            "statement": row["reply_statement"] or "",
            "evidence_refs": _json_loads(row["reply_evidence_json"], []) if "reply_evidence_json" in keys else [],
            "address_signature_verified": bool(row["reply_signature_verified"]) if "reply_signature_verified" in keys else False,
            "statement_hash": row["reply_statement_hash"] if "reply_statement_hash" in keys else "",
            "evidence_hash": row["reply_evidence_hash"] if "reply_evidence_hash" in keys else "",
            "reply_created_at": row["reply_created_at"] if "reply_created_at" in keys else None,
        }

    def _governance_payload_with_latest_dispute_reply_locked(self, conn, row, payload):
        if not isinstance(payload, dict):
            return payload
        dispute_uuid = ""
        reference = str(row["reference"] if "reference" in row.keys() else "").strip()
        prefix = "transaction_dispute:"
        if reference.startswith(prefix):
            dispute_uuid = reference[len(prefix):].strip()
        if not dispute_uuid:
            for claim in payload.get("victim_claims") or []:
                if isinstance(claim, dict) and str(claim.get("claim_id") or "").startswith("pcdispute:"):
                    dispute_uuid = str(claim.get("claim_id") or "").strip()
                    break
        if not dispute_uuid:
            return payload
        dispute_row = conn.execute(
            "SELECT * FROM points_chain_transaction_disputes WHERE dispute_uuid=?",
            (dispute_uuid,),
        ).fetchone()
        if not dispute_row:
            return payload
        reply = self._dispute_reply_payload_from_row(
            dispute_row,
            wallet_address=str(dispute_row["to_wallet_address"] or dispute_row["suspect_wallet_address"] or "").strip().lower(),
        )
        if not reply:
            return payload
        return {**payload, "counterparty_reply": reply, "counterparty_reply_source": "latest_dispute_reply_overlay_v1"}

    def list_transaction_disputes(self, *, actor, limit=50):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._ensure_transaction_dispute_schema(conn)
            is_manager = self._governance_is_manager_actor(actor)
            if is_manager:
                rows = conn.execute(
                    "SELECT dispute_uuid FROM points_chain_transaction_disputes ORDER BY id DESC LIMIT ?",
                    (min(100, max(1, int(limit or 50))),),
                ).fetchall()
            else:
                user_id = int(actor_value(actor, "id") or 0)
                state = self._wallet_identity_state_for_user(conn, user_id) if user_id > 0 else {"addresses": set()}
                addresses = sorted(str(item or "").strip().lower() for item in (state.get("addresses") or set()) if item)
                params = [user_id]
                address_clause = ""
                if addresses:
                    placeholders = ", ".join("?" for _ in addresses)
                    address_clause = f" OR from_wallet_address IN ({placeholders}) OR to_wallet_address IN ({placeholders})"
                    params.extend(addresses)
                    params.extend(addresses)
                params.append(min(100, max(1, int(limit or 50))))
                rows = conn.execute(
                    f"""
                    SELECT dispute_uuid FROM points_chain_transaction_disputes
                    WHERE reporter_user_id=?{address_clause}
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
            disputes = [self._serialize_transaction_dispute(conn, row["dispute_uuid"]) for row in rows]
            addresses = set()
            for item in disputes:
                addresses.add(str((item or {}).get("from_wallet_address") or (item or {}).get("victim_wallet_address") or "").strip().lower())
                addresses.add(str((item or {}).get("to_wallet_address") or (item or {}).get("suspect_wallet_address") or "").strip().lower())
            payload = {"ok": True, "disputes": disputes}
            labels = self._official_hot_wallet_labels_locked(conn, actor=actor, addresses=addresses)
            if labels:
                payload["official_hot_wallet_labels"] = labels
            return payload
        finally:
            conn.close()

    def reply_transaction_dispute(self, *, actor, dispute_uuid, statement, evidence=None, public_key_jwk=None, signature="", signature_nonce="", account_bound_proof=False):
        actor_id = int(actor_value(actor, "id") or 0)
        if actor_id <= 0:
            raise PermissionError("login required")
        if str(actor_value(actor, "username") or "") == "root":
            raise PermissionError("root 帳號不使用匿名地址疑義回覆流程；官方錢包或官方地址事故請改走官方治理、內部帳務治理或緊急安全治理。")
        dispute_uuid = str(dispute_uuid or "").strip()
        if not dispute_uuid:
            raise ValueError("dispute uuid required")
        statement = str(statement or "").strip()
        if len(statement) < 12:
            raise ValueError("reply statement must be at least 12 characters")
        self._assert_dispute_reply_has_no_identity_leak(statement)
        evidence_list = self._governance_evidence(evidence or [])
        for item in evidence_list:
            self._assert_dispute_reply_has_no_identity_leak(str(item or ""))
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._ensure_transaction_dispute_schema(conn)
            row = conn.execute("SELECT * FROM points_chain_transaction_disputes WHERE dispute_uuid=?", (dispute_uuid,)).fetchone()
            if not row:
                raise ValueError("dispute not found")
            dispute_status = str(row["status"] or "")
            if dispute_status not in {"pending_review", "approved", "proposal_created"}:
                raise ValueError("dispute no longer accepts address replies")
            if dispute_status == "proposal_created":
                proposal_uuid = str(row["governance_proposal_uuid"] or "").strip()
                if proposal_uuid:
                    proposal = conn.execute(
                        "SELECT status, lifecycle_status FROM points_chain_governance_proposals WHERE proposal_uuid=?",
                        (proposal_uuid,),
                    ).fetchone()
                    if proposal and str(proposal["status"] or "") in {"executed", "cancelled", "expired", "vetoed", "failed"}:
                        raise ValueError("governance proposal is closed and no longer accepts address replies")
            if int(row["reply_signature_verified"] or 0):
                raise ValueError("address dispute reply already exists")
            to_address = str(row["to_wallet_address"] or row["suspect_wallet_address"] or "").strip().lower()
            from_address = str(row["from_wallet_address"] or row["victim_wallet_address"] or "").strip().lower()
            if not WALLET_ADDRESS_RE.fullmatch(to_address) or not WALLET_ADDRESS_RE.fullmatch(from_address):
                raise ValueError("dispute is missing address scope")
            statement_hash = self._address_dispute_statement_hash(statement)
            evidence_hash = self._address_dispute_evidence_hash(evidence_list)
            runtime_mode = str(row["signature_runtime_mode"] or self._address_dispute_runtime_mode()).strip() or self._address_dispute_runtime_mode()
            signature_nonce = str(signature_nonce or "").strip()
            signature_value = str(signature or "")
            canonical_jwk = {}
            reply_identity_model = "address_proven_anonymous_v1"
            reply_purpose = "address_dispute_reply"
            signed_proof = bool(public_key_jwk and signature and signature_nonce)
            if signed_proof:
                verify_wallet_address_dispute_signature(
                    tx_hash=row["tx_hash"],
                    from_wallet_address=from_address,
                    to_wallet_address=to_address,
                    amount_points=int(row["claimed_amount_points"] or 0),
                    statement_hash=statement_hash,
                    evidence_hash=evidence_hash,
                    nonce=signature_nonce,
                    chain_branch=row["chain_branch"] or "main",
                    purpose="address_dispute_reply",
                    signer_wallet_address=to_address,
                    public_key_jwk=public_key_jwk,
                    signature=signature,
                    runtime_mode=runtime_mode,
                )
                reply_signed_payload_hash = self._address_dispute_payload_hash(
                    tx_hash=row["tx_hash"],
                    from_wallet_address=from_address,
                    to_wallet_address=to_address,
                    amount_points=int(row["claimed_amount_points"] or 0),
                    statement_hash=statement_hash,
                    evidence_hash=evidence_hash,
                    nonce=signature_nonce,
                    chain_branch=row["chain_branch"] or "main",
                    purpose="address_dispute_reply",
                    runtime_mode=runtime_mode,
                )
                reply_signature_hash = sha256_text(str(signature or "").strip())
                canonical_jwk = canonical_public_jwk(public_key_jwk)
            else:
                official_hot_wallet = self._account_bound_official_hot_wallet_for_actor_locked(
                    conn,
                    actor_id=actor_id,
                    wallet_address=to_address,
                )
                if not official_hot_wallet:
                    raise ValueError("address-signed reply proof is required unless To is your account-bound official hot wallet")
                if not signature_nonce:
                    signature_nonce = f"account-bound:{uuid.uuid4()}"
                reply_identity_model = "account_bound_official_hot_v1"
                reply_purpose = "account_bound_dispute_reply"
                account_bound_payload = {
                    "amount_points": int(row["claimed_amount_points"] or 0),
                    "chain_branch": row["chain_branch"] or "main",
                    "custody_mode": "server_hot",
                    "evidence_hash": evidence_hash,
                    "from_wallet_address": from_address,
                    "nonce": signature_nonce,
                    "purpose": reply_purpose,
                    "runtime_mode": runtime_mode,
                    "statement_hash": statement_hash,
                    "to_wallet_address": to_address,
                    "tx_hash": row["tx_hash"],
                    "wallet_type": "official_hot",
                }
                reply_signed_payload_hash = sha256_text(canonical_json(account_bound_payload))
                reply_signature_hash = sha256_text(f"account_bound_official_hot_v1:{to_address}:{reply_signed_payload_hash}")
                signature_value = "account_bound_official_hot_v1"
            self._assert_address_dispute_signature_not_replayed_locked(
                conn,
                signed_payload_hash=reply_signed_payload_hash,
                signature_hash=reply_signature_hash,
                exclude_dispute_uuid=dispute_uuid,
            )
            now = utc_now()
            conn.execute(
                """
                UPDATE points_chain_transaction_disputes
                SET reply_statement=?, reply_evidence_json=?, reply_statement_hash=?,
                    reply_evidence_hash=?, reply_nonce=?, reply_signature=?,
                    reply_signed_payload_hash=?, reply_signature_hash=?,
                    reply_public_key_jwk_json=?, reply_signature_verified=1,
                    reply_created_at=?, updated_at=?
                WHERE dispute_uuid=?
                """,
                (
                    statement[:4000],
                    _json_dumps(evidence_list),
                    statement_hash,
                    evidence_hash,
                    signature_nonce[:120],
                    signature_value,
                    reply_signed_payload_hash,
                    reply_signature_hash,
                    canonical_json(canonical_jwk),
                    now,
                    now,
                    dispute_uuid,
                ),
            )
            self._audit_log(
                conn,
                "POINTS_CHAIN_ADDRESS_DISPUTE_REPLY",
                "info",
                "Address-proven transaction dispute reply submitted",
                actor=None,
                metadata={
                    "dispute_uuid": dispute_uuid,
                    "tx_hash": row["tx_hash"],
                    "from_wallet_address": from_address,
                    "to_wallet_address": to_address,
                    "identity_redaction_model": reply_identity_model,
                    "signature_purpose": reply_purpose,
                },
            )
            conn.commit()
            return {"ok": True, "dispute": self._serialize_transaction_dispute(conn, dispute_uuid)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def review_transaction_dispute(self, *, actor, dispute_uuid, status, review_note="", recommended_strategy="", create_proposal=False):
        if not self._governance_is_manager_actor(actor):
            raise PermissionError("manager+ required")
        status = str(status or "").strip().lower()
        if status not in {"approved", "rejected", "cancelled"}:
            raise ValueError("review status must be approved, rejected, or cancelled")
        strategy = str(recommended_strategy or "tainted_remainder_return").strip().lower()
        if strategy not in {"treasury_compensation", "tainted_remainder_return", "exclude_tainted_descendants"}:
            raise ValueError("invalid recovery strategy")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._ensure_transaction_dispute_schema(conn)
            now = utc_now()
            conn.execute(
                """
                UPDATE points_chain_transaction_disputes
                SET status=?, reviewed_by=?, reviewed_at=?, review_note=?, recommended_strategy=?, updated_at=?
                WHERE dispute_uuid=?
                """,
                (status, int(actor_value(actor, "id") or 0), now, str(review_note or "")[:1000], strategy, now, dispute_uuid),
            )
            row = self._serialize_transaction_dispute(conn, dispute_uuid)
            conn.commit()
            proposal = None
            address_risk_proposal = None
            address_freeze_proposal = None
            provisional_freeze = None
            provisional_release = None
            taint_exposure = None
            if status == "approved" and create_proposal and row:
                suspect_address = str(row.get("to_wallet_address") or row.get("suspect_wallet_address") or "").strip().lower()
                victim_address = str(row.get("from_wallet_address") or row.get("victim_wallet_address") or "").strip().lower()
                if not WALLET_ADDRESS_RE.fullmatch(suspect_address):
                    suspect_address = self._dispute_suspect_address_from_tx(row["tx_hash"], victim_address)
                taint_exposure = self._trading_product_exposure_for_addresses_locked(
                    conn,
                    [suspect_address] if suspect_address else [],
                )
                victim_claim = {
                    "claim_id": dispute_uuid,
                    "wallet_address": victim_address,
                    "claim_amount_points": row["claimed_amount_points"],
                    "statement": row["statement"],
                    "evidence_refs": row["evidence"],
                    "review_status": "approved",
                    "review_note": review_note,
                    "address_signature_verified": bool(row.get("from_signature_verified")),
                    "statement_hash": row.get("statement_hash") or "",
                    "evidence_hash": row.get("evidence_hash") or "",
                }
                counterparty_reply = self._dispute_reply_payload_from_row(
                    conn.execute("SELECT * FROM points_chain_transaction_disputes WHERE dispute_uuid=?", (dispute_uuid,)).fetchone(),
                    wallet_address=suspect_address,
                ) if row.get("reply_statement") else {}
                proposal = self.create_emergency_recovery_branch_proposal(
                    actor=actor,
                    incident_tx_hash=row["tx_hash"],
                    reason=review_note or f"disputed transaction review approved: {dispute_uuid}",
                    excluded_tx_hashes=[row["tx_hash"]],
                    recovery_strategy=strategy,
                    loss_cause=row["loss_cause"],
                    victim_statement=row["statement"],
                    victim_evidence_refs=row["evidence"],
                    victim_claims=[victim_claim] if victim_address and row["claimed_amount_points"] else [],
                    product_exposure=taint_exposure,
                    counterparty_reply=counterparty_reply,
                    reference=f"transaction_dispute:{dispute_uuid}",
                )
                if suspect_address:
                    address_risk_proposal = self.create_address_risk_proposal(
                        actor=actor,
                        wallet_address=suspect_address,
                        reason=f"disputed transaction approved: {dispute_uuid}; {review_note or row['statement']}",
                        evidence=[row["tx_hash"], *list(row["evidence"] or [])],
                        reference=f"transaction_dispute:{dispute_uuid}:address_risk",
                    )
                    address_freeze_proposal = self.create_wallet_freeze_proposal(
                        actor=actor,
                        wallet_address=suspect_address,
                        reason=f"freeze outgoing transfer pending public governance review for disputed transaction: {dispute_uuid}",
                        evidence=[row["tx_hash"], *list(row["evidence"] or [])],
                        reference=f"transaction_dispute:{dispute_uuid}:address_freeze",
                        release=False,
                    )
                conn2 = self.get_db()
                try:
                    self.ensure_schema(conn2)
                    self._ensure_transaction_dispute_schema(conn2)
                    freeze_proposal_uuid = (address_freeze_proposal.get("proposal") or {}).get("proposal_uuid") if address_freeze_proposal else ""
                    if suspect_address:
                        provisional_freeze = self._create_provisional_address_freeze_locked(
                            conn2,
                            wallet_address=suspect_address,
                            reason=f"temporary freeze during governance review for disputed transaction: {dispute_uuid}",
                            evidence=[row["tx_hash"], *list(row["evidence"] or [])],
                            source_dispute_uuid=dispute_uuid,
                            linked_proposal_uuid=freeze_proposal_uuid,
                            actor=actor,
                            ttl_seconds=DISPUTE_ESCALATED_FREEZE_SECONDS,
                        )
                    escalation_expires_at = provisional_freeze.get("expires_at") if provisional_freeze else None
                    voting_deadline = self._governance_time_after(DISPUTE_ESCALATED_FREEZE_SECONDS - DISPUTE_VOTING_GRACE_SECONDS)
                    if freeze_proposal_uuid:
                        self._update_governance_deadline_guard_locked(
                            conn2,
                            freeze_proposal_uuid,
                            voting_ends_at=voting_deadline,
                            expires_at=voting_deadline,
                        )
                    conn2.execute(
                        """
                        UPDATE points_chain_transaction_disputes
                        SET status='proposal_created', governance_proposal_uuid=?,
                            suspect_wallet_address=?, address_risk_proposal_uuid=?,
                            address_freeze_proposal_uuid=?, escalated_freeze_expires_at=?, updated_at=?
                        WHERE dispute_uuid=?
                        """,
                        (
                            (proposal.get("proposal") or {}).get("proposal_uuid") or "",
                            suspect_address,
                            (address_risk_proposal.get("proposal") or {}).get("proposal_uuid") if address_risk_proposal else "",
                            freeze_proposal_uuid,
                            escalation_expires_at,
                            utc_now(),
                            dispute_uuid,
                        ),
                    )
                    conn2.commit()
                    row = self._serialize_transaction_dispute(conn2, dispute_uuid)
                finally:
                    conn2.close()
            elif status in {"rejected", "cancelled"} and row:
                conn2 = self.get_db()
                try:
                    self.ensure_schema(conn2)
                    provisional_release = self._release_provisional_address_freeze_locked(
                        conn2,
                        source_dispute_uuid=dispute_uuid,
                        reason=f"transaction dispute {status}: {dispute_uuid}",
                    )
                    if provisional_release.get("released_count"):
                        self._audit_log(
                            conn2,
                            "POINTS_CHAIN_PROVISIONAL_FREEZE_RELEASED",
                            "warning",
                            f"Provisional address freeze released after dispute {status}",
                            actor=actor,
                            metadata={
                                "dispute_uuid": dispute_uuid,
                                "status": status,
                                "release": provisional_release,
                            },
                        )
                    conn2.commit()
                except Exception:
                    conn2.rollback()
                    raise
                finally:
                    conn2.close()
            return {
                "ok": True,
                "dispute": row,
                "proposal": (proposal or {}).get("proposal") if proposal else None,
                "address_risk_proposal": (address_risk_proposal or {}).get("proposal") if address_risk_proposal else None,
                "address_freeze_proposal": (address_freeze_proposal or {}).get("proposal") if address_freeze_proposal else None,
                "provisional_freeze": provisional_freeze,
                "provisional_release": provisional_release,
                "taint_exposure": taint_exposure,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def sponsor_governance_proposal(self, *, actor, proposal_uuid):
        if not self._governance_is_manager_actor(actor):
            raise PermissionError("manager+ required to sponsor governance proposal")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            self._governance_assert_clock_safe_locked(conn, "sponsor_governance_proposal")
            row = self._refresh_governance_proposal_locked(conn, proposal_uuid)
            if not row:
                raise ValueError("proposal not found")
            if row["lifecycle_status"] != "REVIEW":
                raise ValueError(f"proposal is {row['lifecycle_status']} and cannot be sponsored")
            now = utc_now()
            audit = self._append_governance_audit_locked(
                conn,
                proposal_uuid=proposal_uuid,
                event_type="PROPOSAL_SPONSORED",
                actor=actor,
                payload_hash=row["execution_payload_hash"],
                metadata={"governance_domain": row["governance_domain"], "action_type": row["action_type"]},
            )
            conn.execute(
                """
                UPDATE points_chain_governance_proposals
                SET sponsor_user_id=?, sponsor_role=?, sponsored_at=?,
                    lifecycle_status='VOTING', voting_starts_at=COALESCE(voting_starts_at, ?),
                    prev_audit_hash=?, audit_hash=?, updated_at=?
                WHERE proposal_uuid=?
                """,
                (
                    int(actor_value(actor, "id")) if actor_value(actor, "id") else None,
                    self._governance_actor_role(actor),
                    now,
                    now,
                    audit["prev_audit_hash"],
                    audit["audit_hash"],
                    now,
                    proposal_uuid,
                ),
            )
            refreshed = conn.execute("SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?", (proposal_uuid,)).fetchone()
            conn.commit()
            return {"ok": True, "proposal": self._serialize_governance_proposal(conn, refreshed, actor=actor)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _governance_multisig_status_locked(self, conn, row, *, actor=None):
        payload = _json_loads(row["payload_json"], {})
        policy = payload.get("official_multisig_policy") if isinstance(payload.get("official_multisig_policy"), dict) else {}
        actor_id = int(actor_value(actor, "id") or 0)
        current_signer_wallet_address = ""
        if actor_id:
            for signer in policy.get("signers") or []:
                if int(signer.get("user_id") or 0) == actor_id:
                    current_signer_wallet_address = str(signer.get("wallet_address") or "")
                    break
        public_policy = {
            "policy_version": policy.get("policy_version") or "",
            "fund_key": policy.get("fund_key") or "",
            "wallet_type": policy.get("wallet_type") or "official_treasury_multisig",
            "wallet_scope": policy.get("wallet_scope") or "official_treasury",
            "custody_mode": policy.get("custody_mode") or "multisig",
            "spend_capability": policy.get("spend_capability") or "enabled",
            "threshold": int(policy.get("threshold") or 0),
            "threshold_weight": int(policy.get("threshold_weight") or 0),
            "signer_count": int(policy.get("signer_count") or 0),
            "total_weight": int(policy.get("total_weight") or 0),
            "signer_addresses": [str(item or "") for item in (policy.get("signer_addresses") or [])],
            "signers": [
                {
                    "signer_id": item.get("signer_id") or "",
                    "wallet_address": item.get("wallet_address") or "",
                    "wallet_type": item.get("wallet_type") or "",
                    "custody_mode": item.get("custody_mode") or "",
                    "role": item.get("role") or "",
                    "weight": int(item.get("weight") or 0),
                    "device_id": item.get("device_id") or "",
                    "pubkey_fingerprint": item.get("pubkey_fingerprint") or "",
                    "signer_created_at": item.get("signer_created_at") or "",
                    "last_used_at": item.get("last_used_at") or "",
                    "revoked": bool(item.get("revoked")),
                }
                for item in (policy.get("signers") or [])
            ],
            "signature_required_after_governance": bool(policy.get("signature_required_after_governance")),
            "server_hot_attestation_allowed": bool(policy.get("server_hot_attestation_allowed")),
            "signer_identity_model": policy.get("signer_identity_model") or "",
            "signer_weight_model": policy.get("signer_weight_model") or "",
            "device_binding_model": policy.get("device_binding_model") or "",
        }
        if current_signer_wallet_address:
            public_policy["current_signer_wallet_address"] = current_signer_wallet_address
        threshold = int(policy.get("threshold") or 0)
        threshold_weight = int(policy.get("threshold_weight") or 0)
        signer_addresses = {str(item or "").strip().lower() for item in (policy.get("signer_addresses") or []) if str(item or "").strip()}
        signer_weight_by_address = {
            str(item.get("wallet_address") or "").strip().lower(): int(item.get("weight") or 0)
            for item in (policy.get("signers") or [])
        }
        active_signer_addresses = set()
        for address in signer_addresses:
            current_wallet = conn.execute(
                "SELECT status FROM points_wallet_identities WHERE address=? LIMIT 1",
                (address,),
            ).fetchone()
            if current_wallet and current_wallet["status"] in {"pending_backup", "active"}:
                active_signer_addresses.add(address)
        rows = conn.execute(
            """
            SELECT * FROM points_chain_governance_multisig_signatures
            WHERE proposal_uuid=?
            ORDER BY id ASC
            """,
            (row["proposal_uuid"],),
        ).fetchall()
        valid = []
        for sig in rows:
            address = str(sig["signer_wallet_address"] or "").strip().lower()
            if signer_addresses and address not in signer_addresses:
                continue
            if active_signer_addresses and address not in active_signer_addresses:
                continue
            if sig["signed_payload_hash"] != row["execution_payload_hash"]:
                continue
            valid.append(sig)
        signature_weight = sum(signer_weight_by_address.get(str(sig["signer_wallet_address"] or "").strip().lower(), 0) for sig in valid)
        return {
            "required": row["governance_domain"] == "OFFICIAL_TREASURY",
            "policy": public_policy,
            "threshold": threshold,
            "threshold_weight": threshold_weight,
            "signature_count": len(valid),
            "signature_weight": signature_weight,
            "ready": bool(threshold and len(valid) >= threshold and (not threshold_weight or signature_weight >= threshold_weight)),
            "signatures": [
                {
                    "signer_wallet_address": sig["signer_wallet_address"],
                    "signature_mode": sig["signature_mode"],
                    "signed_payload_hash": sig["signed_payload_hash"],
                    "created_at": sig["created_at"],
                }
                for sig in valid
            ],
        }

    def sign_governance_multisig(self, *, actor, proposal_uuid, signer_wallet_address, signature=""):
        if not self._governance_is_manager_actor(actor):
            raise PermissionError("manager+ required to sign official treasury multisig")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            self._governance_assert_clock_safe_locked(conn, "sign_governance_multisig")
            row = self._refresh_governance_proposal_locked(conn, proposal_uuid)
            if not row:
                raise ValueError("proposal not found")
            if row["governance_domain"] != "OFFICIAL_TREASURY":
                raise PermissionError("multisig signing is only required for official treasury proposals")
            if row["status"] != "passed":
                raise ValueError(f"proposal is {row['status']}; multisig signing requires passed governance")
            if int(row["root_veto_used"] or 0):
                raise PermissionError("proposal was vetoed by root")
            if self._governance_execution_payload_hash_for_row(row) != row["execution_payload_hash"]:
                raise ValueError("governance execution payload hash mismatch")
            payload = _json_loads(row["payload_json"], {})
            policy = payload.get("official_multisig_policy") if isinstance(payload.get("official_multisig_policy"), dict) else {}
            signer_address = str(signer_wallet_address or "").strip().lower()
            if signer_address not in {str(item).strip().lower() for item in policy.get("signer_addresses", [])}:
                raise PermissionError("signer wallet is not in official multisig policy")
            status_row = conn.execute(
                "SELECT status FROM points_wallet_identities WHERE address=? LIMIT 1",
                (signer_address,),
            ).fetchone()
            if status_row and status_row["status"] not in {"pending_backup", "active"}:
                raise PermissionError("signer wallet is disabled or revoked")
            wallet = self._wallet_identity_owner_for_address(conn, signer_address)
            if not wallet or int(wallet["user_id"] or 0) != int(actor_value(actor, "id") or 0):
                raise PermissionError("signer wallet is not owned by current actor")
            signature_branch = str(payload.get("chain_branch") or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
            signing_payload = self._governance_multisig_signing_payload(row)
            signing_payload_hash = sha256_text(canonical_json(signing_payload))
            signature_mode = "wallet_signature"
            if str(wallet["custody_mode"] or "") == "self_custody":
                verify_wallet_transaction_signature(
                    user_id=int(actor_value(actor, "id") or 0),
                    source_wallet_address=signer_address,
                    destination_wallet_address=row["target_wallet_address"] or signer_address,
                    amount_points=max(1, int(row["requested_amount"] or 0)),
                    fee_points=0,
                    request_uuid=row["proposal_uuid"],
                    memo=row["execution_payload_hash"],
                    public_key_jwk=_json_loads(wallet["public_key_jwk_json"], {}),
                    signature=signature,
                    chain_branch=signature_branch,
                    proposal_id=row["proposal_uuid"],
                    action_type="points_governance_multisig_sign",
                    payload_hash=row["execution_payload_hash"],
                    signer_key_id=str(wallet["public_key_hash"] or ""),
                )
                signature_hash = sha256_text(str(signature or ""))
            else:
                signature_mode = "server_attested"
                signature_hash = sha256_text(canonical_json({
                    "proposal_uuid": row["proposal_uuid"],
                    "signer_user_id": int(actor_value(actor, "id") or 0),
                    "signer_wallet_address": signer_address,
                    "payload_hash": signing_payload_hash,
                    "mode": signature_mode,
                }))
            now = utc_now()
            conn.execute(
                """
                INSERT INTO points_chain_governance_multisig_signatures (
                    proposal_uuid, signer_user_id, signer_wallet_address, signature_mode,
                    signature_hash, signed_payload_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_uuid, signer_wallet_address)
                DO UPDATE SET signer_user_id=excluded.signer_user_id,
                              signature_mode=excluded.signature_mode,
                              signature_hash=excluded.signature_hash,
                              signed_payload_hash=excluded.signed_payload_hash,
                              created_at=excluded.created_at
                """,
                (
                    row["proposal_uuid"],
                    int(actor_value(actor, "id") or 0),
                    signer_address,
                    signature_mode,
                    signature_hash,
                    row["execution_payload_hash"],
                    now,
                ),
            )
            audit = self._append_governance_audit_locked(
                conn,
                proposal_uuid=row["proposal_uuid"],
                event_type="MULTISIG_SIGNATURE_COLLECTED",
                actor=actor,
                payload_hash=row["execution_payload_hash"],
                metadata={"signer_wallet_address": signer_address, "signature_mode": signature_mode},
            )
            conn.execute(
                "UPDATE points_chain_governance_proposals SET prev_audit_hash=?, audit_hash=?, updated_at=? WHERE proposal_uuid=?",
                (audit["prev_audit_hash"], audit["audit_hash"], now, row["proposal_uuid"]),
            )
            refreshed = conn.execute("SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?", (row["proposal_uuid"],)).fetchone()
            status = self._governance_multisig_status_locked(conn, refreshed)
            conn.commit()
            return {"ok": True, "proposal": self._serialize_governance_proposal(conn, refreshed, actor=actor), "multisig": status}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _refresh_governance_proposal_locked(self, conn, proposal_uuid):
        row = conn.execute(
            "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
            (proposal_uuid,),
        ).fetchone()
        if not row:
            return None
        votes = conn.execute(
            """
            SELECT vote, COUNT(*) AS c
            FROM points_chain_governance_votes
            WHERE proposal_uuid=?
            GROUP BY vote
            """,
            (proposal_uuid,),
        ).fetchall()
        counts = {"yes": 0, "no": 0, "abstain": 0}
        for vote in votes:
            counts[str(vote["vote"])] = int(vote["c"] or 0)
        status = row["status"]
        previous_status = status
        total_votes = sum(counts.values())
        decisive_votes = counts["yes"] + counts["no"]
        lifecycle_status = row["lifecycle_status"]
        if status in {"voting"} and lifecycle_status == "VOTING":
            expired_at = parse_utc_timestamp(row["expires_at"])
            if expired_at and expired_at <= datetime.now(timezone.utc):
                status = "expired"
                lifecycle_status = "EXPIRED"
            elif total_votes >= int(row["quorum_count"] or 0) and decisive_votes > 0:
                approval_units = (counts["yes"] * 10000) // decisive_votes
                differential_units = ((counts["yes"] - counts["no"]) * 10000) // max(1, int(row["eligible_voter_count"] or 0))
                if approval_units >= int(row[GOV_PASS_THRESHOLD_RATE_FIELD] or 0) and differential_units >= int(row[GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD] or 0):
                    status = "passed"
                    timelock_until = parse_utc_timestamp(row["timelock_until"])
                    lifecycle_status = "TIMELOCKED" if timelock_until and timelock_until > datetime.now(timezone.utc) else "QUEUED"
        now = utc_now()
        conn.execute(
            """
            UPDATE points_chain_governance_proposals
            SET yes_count=?, no_count=?, abstain_count=?, status=?, lifecycle_status=?, updated_at=?
            WHERE proposal_uuid=?
            """,
            (counts["yes"], counts["no"], counts["abstain"], status, lifecycle_status, now, proposal_uuid),
        )
        if (
            previous_status != "expired"
            and status == "expired"
            and str(row["action_type"] or "").strip().upper() == "FREEZE_ADDRESS"
        ):
            release = self._release_provisional_address_freeze_locked(
                conn,
                linked_proposal_uuid=proposal_uuid,
                reason=f"governance freeze proposal expired: {proposal_uuid}",
            )
            if release.get("released_count"):
                self._audit_log(
                    conn,
                    "POINTS_CHAIN_PROVISIONAL_FREEZE_RELEASED",
                    "warning",
                    "Provisional address freeze released after governance proposal expired",
                    actor=None,
                    metadata={
                        "proposal_uuid": proposal_uuid,
                        "release": release,
                    },
                )
        return conn.execute(
            "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
            (proposal_uuid,),
        ).fetchone()

    def cast_governance_vote(self, *, actor, proposal_uuid, vote, reason="", recovery_choice=None):
        voter_id = int(actor_value(actor, "id") or 0)
        if voter_id <= 0:
            raise PermissionError("login required")
        vote = str(vote or "").strip().lower()
        if vote not in {"yes", "no", "abstain"}:
            raise ValueError("vote must be yes, no, or abstain")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            self._governance_assert_clock_safe_locked(conn, "cast_governance_vote")
            row = self._refresh_governance_proposal_locked(conn, proposal_uuid)
            if not row:
                raise ValueError("proposal not found")
            if row["status"] != "voting" or row["lifecycle_status"] != "VOTING":
                raise ValueError(f"proposal is {row['lifecycle_status']} and does not accept votes")
            eligible = set(int(item) for item in _json_loads(row["eligible_voters_json"], []))
            if voter_id not in eligible:
                raise PermissionError("not eligible to vote on this proposal")
            stored_reason = str(reason or "").strip()[:500]
            choice = str(recovery_choice or "").strip().lower()
            if str(row["action_type"] or "").strip().upper() == "ROLLBACK_BRANCH" and vote == "yes":
                payload = _json_loads(row["payload_json"], {})
                options = payload.get("recovery_options") if isinstance(payload.get("recovery_options"), dict) else {}
                allowed = set(options.keys()) or {"treasury_compensation", "tainted_remainder_return", "exclude_tainted_descendants"}
                if not choice:
                    choice = str(payload.get("recovery_strategy") or "tainted_remainder_return").strip().lower()
                if choice not in allowed:
                    raise ValueError("invalid recovery option choice")
                stored_reason = _json_dumps({
                    "reason": stored_reason,
                    "recovery_choice": choice,
                })[:500]
            now = utc_now()
            conn.execute(
                """
                INSERT INTO points_chain_governance_votes (
                    proposal_uuid, voter_user_id, vote, reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_uuid, voter_user_id)
                DO UPDATE SET vote=excluded.vote, reason=excluded.reason, updated_at=excluded.updated_at
                """,
                (proposal_uuid, voter_id, vote, stored_reason, now, now),
            )
            refreshed = self._refresh_governance_proposal_locked(conn, proposal_uuid)
            self._audit_log(
                conn,
                "POINTS_CHAIN_GOVERNANCE_VOTE_CAST",
                "info",
                "PointsChain governance vote cast",
                actor=actor,
                metadata={"proposal_uuid": proposal_uuid, "vote": vote, "status": refreshed["status"], "recovery_choice": choice},
            )
            audit = self._append_governance_audit_locked(
                conn,
                proposal_uuid=proposal_uuid,
                event_type="VOTE_CAST",
                actor=actor,
                payload_hash=refreshed["execution_payload_hash"],
                metadata={"vote": vote, "status": refreshed["status"], "lifecycle_status": refreshed["lifecycle_status"], "recovery_choice": choice},
            )
            conn.execute(
                """
                UPDATE points_chain_governance_proposals
                SET prev_audit_hash=?, audit_hash=?
                WHERE proposal_uuid=?
                """,
                (audit["prev_audit_hash"], audit["audit_hash"], proposal_uuid),
            )
            conn.commit()
            return {"ok": True, "proposal": self._serialize_governance_proposal(conn, refreshed, actor=actor)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _address_risk_label_locked(self, conn, address):
        normalized = str(address or "").strip().lower()
        if not normalized:
            return None
        row = conn.execute(
            """
            SELECT * FROM points_chain_address_risk_labels
            WHERE wallet_address=? AND status='active'
            """,
            (normalized,),
        ).fetchone()
        if not row:
            return None
        return {
            "wallet_address": row["wallet_address"],
            "risk_level": row["risk_level"],
            "status": row["status"],
            "label": row["label"],
            "reason": row["reason"],
            "evidence": _json_loads(row["evidence_json"], []),
            "proposal_uuid": row["proposal_uuid"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _address_freeze_locked(self, conn, address):
        normalized = str(address or "").strip().lower()
        if not normalized:
            return None
        row = conn.execute(
            """
            SELECT * FROM points_chain_address_freezes
            WHERE wallet_address=? AND status='active'
            """,
            (normalized,),
        ).fetchone()
        if not row:
            return None
        return {
            "wallet_address": row["wallet_address"],
            "status": row["status"],
            "freeze_type": "governance",
            "reason": row["reason"],
            "evidence": _json_loads(row["evidence_json"], []),
            "freeze_proposal_uuid": row["freeze_proposal_uuid"],
            "release_proposal_uuid": row["release_proposal_uuid"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def _address_provisional_freeze_locked(self, conn, address):
        normalized = str(address or "").strip().lower()
        if not normalized:
            return None
        row = conn.execute(
            """
            SELECT * FROM points_chain_address_provisional_freezes
            WHERE wallet_address=? AND status='active'
            """,
            (normalized,),
        ).fetchone()
        if not row:
            return None
        now = utc_now()
        if str(row["expires_at"] or "") <= now:
            conn.execute(
                """
                UPDATE points_chain_address_provisional_freezes
                SET status='expired', updated_at=?
                WHERE id=? AND status='active'
                """,
                (now, row["id"]),
            )
            self._audit_log(
                conn,
                "POINTS_CHAIN_PROVISIONAL_FREEZE_EXPIRED",
                "info",
                "Provisional address freeze expired automatically",
                actor=None,
                metadata={
                    "wallet_address": row["wallet_address"],
                    "source_dispute_uuid": row["source_dispute_uuid"],
                    "linked_proposal_uuid": row["linked_proposal_uuid"],
                    "expires_at": row["expires_at"],
                },
            )
            return None
        return {
            "wallet_address": row["wallet_address"],
            "status": row["status"],
            "freeze_type": "provisional",
            "reason": row["reason"],
            "evidence": _json_loads(row["evidence_json"], []),
            "source_dispute_uuid": row["source_dispute_uuid"],
            "linked_proposal_uuid": row["linked_proposal_uuid"],
            "reviewed_by": int(row["reviewed_by"] or 0) if row["reviewed_by"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "expires_at": row["expires_at"],
        }

    def _assert_address_not_frozen_for_outgoing_locked(self, conn, address, *, action="wallet spend"):
        normalized = str(address or "").strip().lower()
        if not WALLET_ADDRESS_RE.fullmatch(normalized):
            return
        if self._address_freeze_locked(conn, normalized):
            raise PermissionError(f"{action}: source wallet address is frozen by governance vote")
        if self._address_provisional_freeze_locked(conn, normalized):
            raise PermissionError(f"{action}: source wallet address is temporarily frozen pending governance review")

    def _create_provisional_address_freeze_locked(
        self,
        conn,
        *,
        wallet_address,
        reason,
        evidence=None,
        source_dispute_uuid="",
        linked_proposal_uuid="",
        actor=None,
        ttl_seconds=PROVISIONAL_ADDRESS_FREEZE_SECONDS,
    ):
        normalized = str(wallet_address or "").strip().lower()
        if not WALLET_ADDRESS_RE.fullmatch(normalized):
            raise ValueError("wallet address format is invalid")
        now = utc_now()
        expires_at = self._governance_time_after(max(60, int(ttl_seconds or PROVISIONAL_ADDRESS_FREEZE_SECONDS)))
        conn.execute(
            """
            INSERT INTO points_chain_address_provisional_freezes (
                wallet_address, status, reason, evidence_json, source_dispute_uuid,
                linked_proposal_uuid, reviewed_by, created_at, updated_at, expires_at
            ) VALUES (?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(wallet_address)
            DO UPDATE SET status='active',
                          reason=excluded.reason,
                          evidence_json=excluded.evidence_json,
                          source_dispute_uuid=excluded.source_dispute_uuid,
                          linked_proposal_uuid=excluded.linked_proposal_uuid,
                          reviewed_by=excluded.reviewed_by,
                          updated_at=excluded.updated_at,
                          expires_at=excluded.expires_at,
                          released_at=NULL
            """,
            (
                normalized,
                str(reason or "")[:1000],
                _json_dumps(self._governance_evidence(evidence or [])),
                str(source_dispute_uuid or "")[:128],
                str(linked_proposal_uuid or "")[:128],
                int(actor_value(actor, "id") or 0) or None,
                now,
                now,
                expires_at,
            ),
        )
        return self._address_provisional_freeze_locked(conn, normalized)

    def _release_provisional_address_freeze_locked(self, conn, *, wallet_address="", source_dispute_uuid="", linked_proposal_uuid="", reason=""):
        now = utc_now()
        if wallet_address:
            normalized = str(wallet_address or "").strip().lower()
            if not WALLET_ADDRESS_RE.fullmatch(normalized):
                raise ValueError("wallet address format is invalid")
            cur = conn.execute(
                """
                UPDATE points_chain_address_provisional_freezes
                SET status='released', updated_at=?, released_at=?
                WHERE wallet_address=? AND status='active'
                """,
                (now, now, normalized),
            )
        elif source_dispute_uuid:
            cur = conn.execute(
                """
                UPDATE points_chain_address_provisional_freezes
                SET status='released', updated_at=?, released_at=?
                WHERE source_dispute_uuid=? AND status='active'
                """,
                (now, now, str(source_dispute_uuid or "")[:128]),
            )
        elif linked_proposal_uuid:
            cur = conn.execute(
                """
                UPDATE points_chain_address_provisional_freezes
                SET status='released', updated_at=?, released_at=?
                WHERE linked_proposal_uuid=? AND status='active'
                """,
                (now, now, str(linked_proposal_uuid or "")[:128]),
            )
        else:
            return {"released_count": 0, "reason": reason}
        return {"released_count": int(cur.rowcount or 0), "reason": reason, "released_at": now}

    def veto_governance_proposal(self, *, actor, proposal_uuid, reason=""):
        self._governance_require_root(actor)
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            self._governance_assert_clock_safe_locked(conn, "veto_governance_proposal")
            row = self._refresh_governance_proposal_locked(conn, proposal_uuid)
            if not row:
                raise ValueError("proposal not found")
            if not int(row["root_veto_allowed"] or 0):
                raise PermissionError("root veto is not allowed for this governance domain")
            if row["status"] in {"executed", "expired", "cancelled"} or row["lifecycle_status"] in {"EXECUTED", "EXPIRED", "CANCELLED", "VETOED"}:
                raise ValueError(f"proposal is {row['lifecycle_status']} and cannot be vetoed")
            now = utc_now()
            veto_reason = str(reason or "root veto").strip()[:1000] or "root veto"
            audit = self._append_governance_audit_locked(
                conn,
                proposal_uuid=proposal_uuid,
                event_type="ROOT_VETO",
                actor=actor,
                payload_hash=row["execution_payload_hash"],
                metadata={"reason": veto_reason, "governance_domain": row["governance_domain"], "action_type": row["action_type"]},
            )
            conn.execute(
                """
                UPDATE points_chain_governance_proposals
                SET root_veto_used=1, root_vetoed_by=?, root_vetoed_at=?, root_veto_reason=?,
                    status='cancelled', lifecycle_status='VETOED',
                    prev_audit_hash=?, audit_hash=?, updated_at=?
                WHERE proposal_uuid=?
                """,
                (
                    int(actor_value(actor, "id")) if actor_value(actor, "id") else None,
                    now,
                    veto_reason,
                    audit["prev_audit_hash"],
                    audit["audit_hash"],
                    now,
                    proposal_uuid,
                ),
            )
            self._audit_log(
                conn,
                "POINTS_CHAIN_GOVERNANCE_ROOT_VETO",
                "warning",
                "PointsChain governance proposal vetoed by root",
                actor=actor,
                metadata={"proposal_uuid": proposal_uuid, "reason": veto_reason},
            )
            refreshed = conn.execute("SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?", (proposal_uuid,)).fetchone()
            conn.commit()
            return {"ok": True, "proposal": self._serialize_governance_proposal(conn, refreshed, actor=actor)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cancel_governance_proposal(self, *, actor, proposal_uuid, reason=""):
        if not self._governance_is_manager_actor(actor):
            raise PermissionError("manager+ required")
        cancel_reason = str(reason or "proposal cancelled").strip()[:1000] or "proposal cancelled"
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            self._governance_assert_clock_safe_locked(conn, "cancel_governance_proposal")
            row = self._refresh_governance_proposal_locked(conn, proposal_uuid)
            if not row:
                raise ValueError("proposal not found")
            if row["status"] in {"executed", "expired", "cancelled"} or row["lifecycle_status"] in {"EXECUTED", "EXPIRED", "CANCELLED", "VETOED"}:
                raise ValueError(f"proposal is {row['lifecycle_status']} and cannot be cancelled")
            now = utc_now()
            audit = self._append_governance_audit_locked(
                conn,
                proposal_uuid=proposal_uuid,
                event_type="PROPOSAL_CANCELLED",
                actor=actor,
                payload_hash=row["execution_payload_hash"],
                metadata={"reason": cancel_reason, "governance_domain": row["governance_domain"], "action_type": row["action_type"]},
            )
            conn.execute(
                """
                UPDATE points_chain_governance_proposals
                SET status='cancelled', lifecycle_status='CANCELLED',
                    prev_audit_hash=?, audit_hash=?, updated_at=?
                WHERE proposal_uuid=?
                """,
                (audit["prev_audit_hash"], audit["audit_hash"], now, proposal_uuid),
            )
            self._audit_log(
                conn,
                "POINTS_CHAIN_GOVERNANCE_PROPOSAL_CANCELLED",
                "warning",
                "PointsChain governance proposal cancelled",
                actor=actor,
                metadata={"proposal_uuid": proposal_uuid, "reason": cancel_reason},
            )
            refreshed = conn.execute("SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?", (proposal_uuid,)).fetchone()
            conn.commit()
            return {"ok": True, "proposal": self._serialize_governance_proposal(conn, refreshed, actor=actor)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _activate_recovery_branch_locked(self, conn, *, row, proposal_uuid, now):
        branch_uuid = f"pcbranch:{uuid.uuid4()}"
        parent = conn.execute(
            """
            SELECT * FROM points_chain_governance_branches
            WHERE is_canonical=1 ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        parent_branch_uuid = parent["branch_uuid"] if parent else self._main_branch_uuid()
        payload = _json_loads(row["payload_json"], {})
        selected_strategy = self._selected_recovery_strategy_locked(conn, row)
        payload["selected_recovery_strategy"] = selected_strategy
        payload["recovery_strategy"] = selected_strategy
        if parent:
            conn.execute("UPDATE points_chain_governance_branches SET is_canonical=0, write_enabled=0, status='archived' WHERE is_canonical=1")
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO points_chain_governance_branches (
                    branch_uuid, proposal_uuid, parent_branch_uuid, branch_name,
                    base_block_number, base_block_hash, incident_tx_hash,
                    status, is_canonical, write_enabled, recovery_type, replay_plan_json, created_at, activated_at
                ) VALUES ('main', ?, '', 'main', NULL, '', ?, 'archived', 0, 0, 'pre_fork_main', ?, ?, ?)
                """,
                (
                    proposal_uuid,
                    row["incident_tx_hash"],
                    _json_dumps({
                        "asset_universe": "main",
                        "read_only_after_recovery_branch": True,
                        "ledger_mutation": "forbidden",
                    }),
                    now,
                    now,
                ),
            )
        conn.execute(
            """
            INSERT INTO points_chain_governance_branches (
                branch_uuid, proposal_uuid, parent_branch_uuid, branch_name,
                base_block_number, base_block_hash, incident_tx_hash,
                status, is_canonical, write_enabled, recovery_type, replay_plan_json, created_at, activated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'canonical_recovery', 1, 1, 'chain_fork_asset_universe_v1', ?, ?, ?)
            """,
            (
                branch_uuid,
                proposal_uuid,
                parent_branch_uuid,
                f"emergency-recovery-{row['id']}",
                row["base_block_number"],
                row["base_block_hash"],
                row["incident_tx_hash"],
                _json_dumps({
                    **payload,
                    "proposal_uuid": proposal_uuid,
                    "ledger_mutation": "forbidden",
                    "canonical_pointer_changed": True,
                    "asset_universe": branch_uuid,
                    "parent_asset_universe": parent_branch_uuid,
                    "old_branch_read_only": True,
                    "cross_branch_merge_forbidden": True,
                }),
                now,
                now,
            ),
        )
        system_fund_seed = self._seed_recovery_branch_system_funds_locked(
            conn,
            branch_uuid=branch_uuid,
            parent_branch_uuid=parent_branch_uuid,
            recovery_strategy=str(payload.get("recovery_strategy") or "treasury_compensation").strip().lower(),
            actor={"role": "system", "username": "system"},
        )
        recovery_seed = self._seed_recovery_branch_balances_locked(
            conn,
            branch_uuid=branch_uuid,
            parent_branch_uuid=parent_branch_uuid,
            proposal_row=row,
            override_payload=payload,
            actor={"role": "system", "username": "system"},
        )
        recovery_seed["system_funds"] = system_fund_seed
        return {
            "action": "canonical_recovery_branch_activated",
            "branch_uuid": branch_uuid,
            "parent_branch_uuid": parent_branch_uuid,
            "incident_tx_hash": row["incident_tx_hash"],
            "ledger_mutation": "forbidden",
            "asset_universe": branch_uuid,
            "old_branch_read_only": True,
            "cross_branch_merge_forbidden": True,
            "recovery_seed": recovery_seed,
        }

    def _seed_recovery_branch_system_funds_locked(self, conn, *, branch_uuid, parent_branch_uuid, recovery_strategy, actor=None):
        parent_branch = str(parent_branch_uuid or self._main_branch_uuid()).strip() or self._main_branch_uuid()
        branch_uuid = str(branch_uuid or "").strip()
        if not branch_uuid:
            raise ValueError("recovery branch uuid is required")
        parent_economy = replay_economy_events(
            conn,
            chain_secret=self.chain_secret,
            persist_cache=False,
            chain_branch=parent_branch,
        )
        created = []
        skipped = []
        for fund_key in ("burn", "official_treasury", "promo_fund", "exchange_fund"):
            idempotency_key = f"recovery_branch_system_fund_carry_forward:{branch_uuid}:{fund_key}"
            existing = conn.execute(
                "SELECT * FROM points_economy_events WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                created.append({
                    "fund_key": fund_key,
                    "amount_points": int(existing["amount"] or 0),
                    "event_uuid": existing["event_uuid"],
                    "created": False,
                    "source_branch_uuid": _json_loads(existing["metadata_json"], {}).get("source_branch_uuid") or parent_branch,
                })
                continue
            fund_source = self._recovery_branch_system_fund_source_locked(
                conn,
                parent_branch_uuid=parent_branch,
                fund_key=fund_key,
            )
            amount = int(fund_source.get("amount_points") or 0)
            if amount <= 0:
                skipped.append({"fund_key": fund_key, "reason": "zero_parent_balance", "source_branch_uuid": fund_source.get("source_branch_uuid") or parent_branch})
                continue
            event, was_created = append_economy_event(
                conn,
                chain_secret=self.chain_secret,
                event_type="recovery_branch_system_fund_carry_forward",
                transaction_type=f"recovery_branch_{fund_key}_carry_forward",
                source_fund_key="mint",
                destination_fund_key=fund_key,
                amount=amount,
                idempotency_key=idempotency_key,
                metadata={
                    "recovery_branch_uuid": branch_uuid,
                    "parent_branch_uuid": parent_branch,
                    "source_branch_uuid": fund_source.get("source_branch_uuid") or parent_branch,
                    "recovery_strategy": recovery_strategy,
                    "fund_key": fund_key,
                    "source": "parent_branch_system_fund_balance",
                    "branch_accounting": "system_fund_branch_scope_v1",
                    "used_ancestor_fallback": bool(fund_source.get("used_ancestor_fallback")),
                    "parent_exchange_receivable_principal": int(parent_economy.get("exchange_receivable_principal") or 0),
                    "parent_exchange_total_assets": int(parent_economy.get("exchange_total_assets") or 0),
                },
                actor=actor,
                chain_branch=branch_uuid,
            )
            created.append({
                "fund_key": fund_key,
                "amount_points": amount,
                "event_uuid": event["event_uuid"],
                "created": bool(was_created),
                "source_branch_uuid": fund_source.get("source_branch_uuid") or parent_branch,
                "used_ancestor_fallback": bool(fund_source.get("used_ancestor_fallback")),
            })
        return {
            "parent_branch_uuid": parent_branch,
            "branch_uuid": branch_uuid,
            "created": created,
            "created_count": len(created),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "parent_health": parent_economy.get("health") or {},
            "parent_exchange_receivable_principal": int(parent_economy.get("exchange_receivable_principal") or 0),
            "parent_exchange_total_assets": int(parent_economy.get("exchange_total_assets") or 0),
            "model": "system_fund_branch_scope_v1",
        }

    def _recovery_branch_system_fund_source_locked(self, conn, *, parent_branch_uuid, fund_key):
        branch = str(parent_branch_uuid or self._main_branch_uuid()).strip() or self._main_branch_uuid()
        fund_key = str(fund_key or "").strip().lower()
        visited = set()
        first = True
        while branch and branch not in visited:
            visited.add(branch)
            replay = replay_economy_events(
                conn,
                chain_secret=self.chain_secret,
                persist_cache=False,
                chain_branch=branch,
            )
            amount = int(((replay.get("balances") or {}).get(fund_key) or {}).get("balance") or 0)
            has_seed = self._branch_has_system_fund_seed_locked(conn, branch_uuid=branch, fund_key=fund_key)
            if amount > 0 or branch == self._main_branch_uuid() or has_seed:
                return {
                    "source_branch_uuid": branch,
                    "amount_points": amount,
                    "used_ancestor_fallback": not first,
                    "source_branch_has_seed": bool(has_seed),
                }
            parent = conn.execute(
                """
                SELECT parent_branch_uuid FROM points_chain_governance_branches
                WHERE branch_uuid=? LIMIT 1
                """,
                (branch,),
            ).fetchone()
            branch = str(parent["parent_branch_uuid"] or self._main_branch_uuid()).strip() if parent else self._main_branch_uuid()
            first = False
        return {"source_branch_uuid": parent_branch_uuid, "amount_points": 0, "used_ancestor_fallback": False}

    def _branch_has_system_fund_seed_locked(self, conn, *, branch_uuid, fund_key):
        fund_key = str(fund_key or "").strip().lower()
        transaction_types = [f"recovery_branch_{fund_key}_carry_forward"]
        if fund_key == "official_treasury":
            transaction_types.append("recovery_branch_official_treasury_carry_forward")
        placeholders = ", ".join("?" for _ in transaction_types)
        row = conn.execute(
            f"""
            SELECT 1 FROM points_economy_events
            WHERE chain_branch=? AND transaction_type IN ({placeholders})
            LIMIT 1
            """,
            (branch_uuid, *transaction_types),
        ).fetchone()
        return bool(row)

    def _selected_recovery_strategy_locked(self, conn, row):
        payload = _json_loads(row["payload_json"], {})
        options = payload.get("recovery_options") if isinstance(payload.get("recovery_options"), dict) else {}
        allowed = set(options.keys()) or {"treasury_compensation", "tainted_remainder_return", "exclude_tainted_descendants"}
        default = str(payload.get("recovery_strategy") or "tainted_remainder_return").strip().lower()
        if default not in allowed:
            default = "tainted_remainder_return" if "tainted_remainder_return" in allowed else sorted(allowed)[0]
        rows = conn.execute(
            "SELECT reason FROM points_chain_governance_votes WHERE proposal_uuid=? AND vote='yes'",
            (row["proposal_uuid"],),
        ).fetchall()
        counts = {}
        for vote_row in rows:
            reason_payload = _json_loads(vote_row["reason"], {})
            choice = ""
            if isinstance(reason_payload, dict):
                choice = str(reason_payload.get("recovery_choice") or "").strip().lower()
            if not choice:
                choice = str(vote_row["reason"] or "").strip().lower()
            if choice in allowed:
                counts[choice] = int(counts.get(choice, 0)) + 1
        if not counts:
            return default
        ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            raise ValueError("recovery option vote is tied; execution requires a clear option majority")
        return ranked[0][0]

    def _replay_branch_address_balances_locked(self, conn, *, branch_uuid, excluded_refs=None, exclude_tainted_descendants=True):
        branch = str(branch_uuid or self._main_branch_uuid())
        excluded = {str(item or "").strip() for item in (excluded_refs or []) if str(item or "").strip()}
        rows = conn.execute(
            """
            SELECT * FROM points_ledger
            WHERE status='confirmed' AND chain_branch=?
            ORDER BY id ASC
            """,
            (branch,),
        ).fetchall()

        def ledger_refs(ledger):
            return {
                str(ledger["ledger_uuid"] or ""),
                str(ledger["ledger_hash"] or ""),
                str(ledger["reference_id"] or ""),
            }

        auto_excluded = {}
        balances = {}
        skipped = 0
        iterations = 0
        for _index in range(len(rows) + 1):
            iterations += 1
            balances = {}
            skipped = 0
            added_exclusion = False
            for ledger in rows:
                refs = ledger_refs(ledger)
                if refs & excluded:
                    skipped += 1
                    continue
                flow = self._ledger_wallet_flow_for_read(conn, ledger)
                amount = int(ledger["amount"] or 0)
                direction = str(ledger["direction"] or "")
                source = str(flow.get("source_wallet_address") or "").strip().lower()
                destination = str(flow.get("destination_wallet_address") or "").strip().lower()
                if direction in {"credit", "transfer_in"} and destination:
                    balances[destination] = int(balances.get(destination, 0)) + amount
                    continue
                if direction in {"debit", "transfer_out", "reverse", "freeze"} and source:
                    balance_before = int(balances.get(source, 0))
                    balance_after = balance_before - amount
                    if exclude_tainted_descendants and balance_after < 0:
                        reference_id = str(ledger["reference_id"] or ledger["ledger_hash"] or ledger["ledger_uuid"])
                        new_refs = {ref for ref in refs if ref}
                        excluded.update(new_refs)
                        auto_excluded.setdefault(reference_id, {
                            "reference_id": reference_id,
                            "ledger_uuid": ledger["ledger_uuid"],
                            "ledger_hash": ledger["ledger_hash"],
                            "wallet_address": source,
                            "direction": direction,
                            "action_type": ledger["action_type"],
                            "amount": amount,
                            "balance_before": balance_before,
                            "balance_after": balance_after,
                            "reason": "dependent_transaction_would_overdraw_after_recovery_exclusions",
                        })
                        added_exclusion = True
                        break
                    balances[source] = balance_after
                    continue
                if direction == "unfreeze" and (source or destination):
                    address = source or destination
                    balances[address] = int(balances.get(address, 0)) + amount
            if not added_exclusion:
                break
        return {
            "balances": balances,
            "ledger_rows": len(rows),
            "excluded_rows": skipped,
            "excluded_refs": sorted(excluded),
            "auto_excluded_refs": sorted(auto_excluded.keys()),
            "auto_excluded": list(auto_excluded.values())[:100],
            "auto_excluded_count": len(auto_excluded),
            "exclusion_iterations": iterations,
        }

    def _recovery_compensation_policy(self, *, loss_cause, requested_rate_per_10000=None):
        cause = str(loss_cause or "protocol_fault").strip().lower()
        policies = {
            "protocol_fault": {"default_rate_per_10000": 10000, "max_rate_per_10000": 10000, "label": "protocol fault"},
            "chain_bug": {"default_rate_per_10000": 10000, "max_rate_per_10000": 10000, "label": "chain bug"},
            "exchange_bug": {"default_rate_per_10000": 10000, "max_rate_per_10000": 10000, "label": "exchange bug"},
            "treasury_hot_wallet_compromise": {"default_rate_per_10000": 10000, "max_rate_per_10000": 10000, "label": "treasury hot wallet compromise"},
            "admin_negligence": {"default_rate_per_10000": 5000, "max_rate_per_10000": 10000, "label": "admin negligence"},
            "user_phishing": {"default_rate_per_10000": 0, "max_rate_per_10000": 5000, "label": "user phishing"},
            "private_key_leak": {"default_rate_per_10000": 0, "max_rate_per_10000": 5000, "label": "private key leak"},
            "user_negligence": {"default_rate_per_10000": 0, "max_rate_per_10000": 5000, "label": "user negligence"},
            "unknown": {"default_rate_per_10000": 0, "max_rate_per_10000": 5000, "label": "unknown cause"},
        }
        if cause not in policies:
            raise ValueError("unsupported loss cause")
        policy = policies[cause]
        if requested_rate_per_10000 is None or requested_rate_per_10000 == "":
            rate = int(policy["default_rate_per_10000"])
            override = False
        else:
            rate = max(0, min(10000, int(requested_rate_per_10000)))
            override = True
        if rate > int(policy["max_rate_per_10000"]):
            raise ValueError("compensation rate exceeds loss-cause policy cap")
        return {
            "loss_cause": cause,
            "label": policy["label"],
            "compensation_rate_per_10000": rate,
            "max_compensation_rate_per_10000": int(policy["max_rate_per_10000"]),
            "requested_override": override,
            "moral_hazard_control": "user-caused losses default to no compensation and are capped at partial compensation",
        }

    def _normalize_recovery_claims(self, *, victim_claims=None, default_statement="", default_evidence_refs=None):
        claims = victim_claims if isinstance(victim_claims, list) else []
        normalized = []
        for index, raw in enumerate(claims):
            if not isinstance(raw, dict):
                continue
            wallet_address = str(raw.get("wallet_address") or raw.get("victim_wallet_address") or "").strip().lower()
            if not WALLET_ADDRESS_RE.fullmatch(wallet_address):
                raise ValueError("victim claim wallet address format is invalid")
            amount = int(raw.get("claim_amount_points") or raw.get("amount_points") or raw.get("amount") or 0)
            if amount <= 0:
                raise ValueError("victim claim amount must be positive")
            review_status = str(raw.get("review_status") or raw.get("status") or "pending").strip().lower()
            if review_status not in {"pending", "approved", "rejected"}:
                raise ValueError("victim claim review status is invalid")
            normalized.append({
                "claim_id": str(raw.get("claim_id") or f"claim-{index + 1}")[:80],
                "wallet_address": wallet_address,
                "claim_amount_points": amount,
                "statement": str(raw.get("statement") or default_statement or "").strip()[:2000],
                "evidence_refs": self._governance_evidence(raw.get("evidence_refs") or default_evidence_refs or []),
                "review_status": review_status,
                "reviewed_by": str(raw.get("reviewed_by") or "").strip()[:80],
                "review_note": str(raw.get("review_note") or "").strip()[:500],
            })
        return normalized

    def _recovery_branch_compensation_plan_locked(self, conn, *, parent_branch_uuid, excluded_refs, replay_balances):
        return self._recovery_branch_compensation_plan_locked_with_rate(
            conn,
            parent_branch_uuid=parent_branch_uuid,
            excluded_refs=excluded_refs,
            replay_balances=replay_balances,
            compensation_rate_per_10000=10000,
        )

    def _recovery_branch_compensation_plan_locked_with_rate(self, conn, *, parent_branch_uuid, excluded_refs, replay_balances, compensation_rate_per_10000):
        rate_per_10000 = max(0, min(10000, int(compensation_rate_per_10000 or 0)))
        items = []
        gross_total = 0
        for address, balance in sorted((replay_balances or {}).items()):
            balance = int(balance or 0)
            if balance >= 0 or not WALLET_ADDRESS_RE.fullmatch(address):
                continue
            owner = self._wallet_identity_owner_for_address(conn, address)
            if not owner or not owner["user_id"]:
                continue
            gross = abs(balance)
            amount = (gross * rate_per_10000 + 9999) // 10000 if rate_per_10000 else 0
            gross_total += gross
            items.append({
                "wallet_address": address,
                "user_id": int(owner["user_id"]),
                "gross_shortfall_points": gross,
                "shortfall_points": amount,
                "compensation_rate_per_10000": rate_per_10000,
                "post_replay_balance_before_compensation": balance,
                "reason": "successor_transactions_preserved_after_incident_exclusion",
            })
        return {
            "strategy": "treasury_compensation",
            "gross_shortfall_total": gross_total,
            "required_total": sum(int(item["shortfall_points"]) for item in items),
            "item_count": len(items),
            "items": items,
            "excluded_transactions": sorted({str(item or "").strip() for item in (excluded_refs or []) if str(item or "").strip()}),
            "note": "successor transactions stay replayed; official treasury compensates incident sender shortfall",
        }

    def _recovery_branch_tainted_remainder_plan_locked(self, conn, *, parent_branch_uuid, incident_tx_hash="", incident_tx_hashes=None, replay_balances, victim_claims=None):
        incident_refs = {
            str(item or "").strip()
            for item in ([incident_tx_hash] + list(incident_tx_hashes or []))
            if str(item or "").strip()
        }
        if not incident_refs:
            return {"strategy": "tainted_remainder_return", "return_amount": 0, "items": [], "item_count": 0}
        rows = conn.execute(
            """
            SELECT * FROM points_ledger
            WHERE status='confirmed' AND chain_branch=?
            ORDER BY id ASC
            """,
            (str(parent_branch_uuid or self._main_branch_uuid()),),
        ).fetchall()
        incident_rows = [
            row for row in rows
            if incident_refs.intersection({
                str(row["ledger_uuid"] or ""),
                str(row["ledger_hash"] or ""),
                str(row["reference_id"] or ""),
            })
        ]
        incident_out_ids = set()
        tainted_remaining_by_wallet = {}
        stolen_by_victim = {}
        tainted_sources = []
        for row in incident_rows:
            flow = self._ledger_wallet_flow_for_read(conn, row)
            direction = str(row["direction"] or "")
            if direction == "transfer_out":
                source_address = str(flow.get("source_wallet_address") or "").strip().lower()
                destination_address = str(flow.get("destination_wallet_address") or "").strip().lower()
                amount = int(row["amount"] or 0)
                if source_address and destination_address and amount > 0:
                    incident_out_ids.add(int(row["id"] or 0))
                    stolen_by_victim[source_address] = int(stolen_by_victim.get(source_address, 0) or 0) + amount
                    tainted_remaining_by_wallet[destination_address] = int(tainted_remaining_by_wallet.get(destination_address, 0) or 0) + amount
                    tainted_sources.append({
                        "incident_tx_hash": str(row["ledger_hash"] or row["reference_id"] or row["ledger_uuid"] or ""),
                        "ledger_uuid": str(row["ledger_uuid"] or ""),
                        "victim_wallet_address": source_address,
                        "tainted_wallet_address": destination_address,
                        "stolen_amount_points": amount,
                        "ledger_row_id": int(row["id"] or 0),
                    })
        if not tainted_sources:
            return {
                "strategy": "tainted_remainder_return",
                "return_amount": 0,
                "items": [],
                "item_count": 0,
                "incident_tx_hashes": sorted(incident_refs),
                "reason": "incident transfer flow could not be identified",
            }
        payload_claims = self._normalize_recovery_claims(victim_claims=victim_claims or [])
        approved_claims = [claim for claim in payload_claims if claim.get("review_status") == "approved"]
        spent_by_tainted_wallet = {address: 0 for address in tainted_remaining_by_wallet.keys()}
        for row in rows:
            flow = self._ledger_wallet_flow_for_read(conn, row)
            direction = str(row["direction"] or "")
            if direction == "transfer_out" and int(row["id"] or 0) in incident_out_ids:
                continue
            source = str(flow.get("source_wallet_address") or "").strip().lower()
            if source not in tainted_remaining_by_wallet:
                continue
            if direction in {"transfer_out", "debit", "reverse", "freeze"}:
                spent = min(int(tainted_remaining_by_wallet.get(source, 0) or 0), int(row["amount"] or 0))
                tainted_remaining_by_wallet[source] = int(tainted_remaining_by_wallet.get(source, 0) or 0) - spent
                spent_by_tainted_wallet[source] = int(spent_by_tainted_wallet.get(source, 0) or 0) + spent
        return_sources = []
        for address, tainted_remaining in sorted(tainted_remaining_by_wallet.items()):
            wallet_balance = int((replay_balances or {}).get(address, 0) or 0)
            return_amount = max(0, min(int(tainted_remaining or 0), wallet_balance))
            if return_amount > 0:
                return_sources.append({
                    "wallet_address": address,
                    "tainted_remaining_points": int(tainted_remaining or 0),
                    "wallet_balance_points": wallet_balance,
                    "return_amount_points": return_amount,
                    "spent_after_incident_points": int(spent_by_tainted_wallet.get(address, 0) or 0),
                })
        return_amount = sum(int(item["return_amount_points"]) for item in return_sources)
        distribution = self._proportional_tainted_remainder_distribution(
            approved_claims=approved_claims,
            return_amount=return_amount,
        )
        stolen_total = sum(int(item["stolen_amount_points"]) for item in tainted_sources)
        item = {
            "incident_tx_hashes": sorted(incident_refs),
            "stolen_amount_points": stolen_total,
            "stolen_by_victim": [{"wallet_address": key, "amount_points": value} for key, value in sorted(stolen_by_victim.items())],
            "tainted_sources": tainted_sources,
            "tainted_wallets": return_sources,
            "return_amount_points": return_amount,
            "loss_absorbed_by_victim_points": max(0, stolen_total - return_amount),
            "successor_transactions_preserved": True,
            "official_treasury_compensation_points": 0,
            "taint_model": "account_level_taint_first_spend_v1",
            "claims_total_points": sum(int(claim["claim_amount_points"]) for claim in approved_claims),
            "approved_claims": approved_claims,
            "rejected_or_pending_claims": [claim for claim in payload_claims if claim.get("review_status") != "approved"],
            "distribution": distribution,
        }
        return {
            "strategy": "tainted_remainder_return",
            "return_amount": return_amount,
            "item_count": len(tainted_sources),
            "items": [item],
            "return_sources": return_sources,
            "distribution": distribution,
            "note": "user-caused losses only return tainted value still left in the attacker wallet; no treasury compensation and no clawback from downstream recipients",
        }

    def _proportional_tainted_remainder_distribution(self, *, approved_claims, return_amount):
        amount = max(0, int(return_amount or 0))
        if amount <= 0:
            return []
        total = sum(max(0, int(claim.get("claim_amount_points") or 0)) for claim in (approved_claims or []))
        if total <= 0:
            return []
        distribution = []
        allocated = 0
        for claim in approved_claims:
            claim_amount = max(0, int(claim.get("claim_amount_points") or 0))
            share = (amount * claim_amount) // total
            allocated += share
            distribution.append({
                "claim_id": claim.get("claim_id") or "",
                "wallet_address": claim.get("wallet_address") or "",
                "claim_amount_points": claim_amount,
                "allocated_points": share,
            })
        remainder = amount - allocated
        if remainder > 0 and distribution:
            order = sorted(
                range(len(distribution)),
                key=lambda idx: (
                    -((amount * int(distribution[idx]["claim_amount_points"])) % total),
                    distribution[idx]["wallet_address"],
                ),
            )
            for idx in order[:remainder]:
                distribution[idx]["allocated_points"] += 1
        return [item for item in distribution if int(item.get("allocated_points") or 0) > 0]

    def _trading_product_exposure_for_addresses_locked(self, conn, addresses):
        normalized = sorted({
            str(address or "").strip().lower()
            for address in (addresses or [])
            if WALLET_ADDRESS_RE.fullmatch(str(address or "").strip().lower())
        })
        if not normalized:
            return {"addresses": [], "orders": [], "spot_positions": [], "margin_positions": [], "bots": [], "summary": {}}
        placeholders = ", ".join("?" for _ in normalized)
        orders = []
        if table_columns(conn, "trading_orders"):
            try:
                orders = [
                    {
                        "order_uuid": row["order_uuid"],
                        "user_id": int(row["user_id"] or 0),
                        "market_symbol": row["market_symbol"],
                        "side": row["side"],
                        "status": row["status"],
                        "funding_mode": row["funding_mode"] if "funding_mode" in row.keys() else "",
                        "source_wallet_address": row["source_wallet_address"] if "source_wallet_address" in row.keys() else "",
                        "chain_frozen_points": int(row["chain_frozen_points"] or 0) if "chain_frozen_points" in row.keys() else 0,
                        "created_at": row["created_at"],
                    }
                    for row in conn.execute(
                        f"""
                        SELECT * FROM trading_orders
                        WHERE lower(source_wallet_address) IN ({placeholders})
                        ORDER BY id DESC LIMIT 100
                        """,
                        tuple(normalized),
                    ).fetchall()
                ]
            except Exception:
                orders = []
        spot_positions = []
        if table_columns(conn, "trading_spot_positions"):
            try:
                rows = conn.execute(
                    "SELECT * FROM trading_spot_positions ORDER BY user_id ASC, market_symbol ASC"
                ).fetchall()
                for row in rows:
                    source = str(row["source_wallet_address"] if "source_wallet_address" in row.keys() else "").strip().lower()
                    funding_sources = _json_loads(row["funding_sources_json"] if "funding_sources_json" in row.keys() else "[]", [])
                    matched_sources = []
                    for item in funding_sources if isinstance(funding_sources, list) else []:
                        if not isinstance(item, dict):
                            continue
                        address = str(item.get("wallet_address") or "").strip().lower()
                        if address in normalized:
                            matched_sources.append(item)
                    taint_status = str(row["taint_status"] if "taint_status" in row.keys() else "").strip().lower()
                    should_use_legacy_source = (
                        "funding_sources_json" not in row.keys()
                        or taint_status in {"source_traced", "under_review", "tainted"}
                    )
                    if source in normalized and not matched_sources and should_use_legacy_source:
                        matched_sources.append({"wallet_address": source, "legacy_position_source": True})
                    if not matched_sources:
                        continue
                    spot_positions.append({
                        "user_id": int(row["user_id"] or 0),
                        "market_symbol": row["market_symbol"],
                        "quantity_units": int(row["quantity_units"] or 0),
                        "locked_quantity_units": int(row["locked_quantity_units"] or 0),
                        "avg_cost_points": row["avg_cost_points"],
                        "source_wallet_address": source,
                        "taint_status": row["taint_status"] if "taint_status" in row.keys() else "unknown",
                        "taint_source_tx_hash": row["taint_source_tx_hash"] if "taint_source_tx_hash" in row.keys() else "",
                        "matched_funding_sources": matched_sources[:20],
                    })
            except Exception:
                spot_positions = []
        margin_refs = {}
        try:
            for row in conn.execute(
                f"""
                SELECT ledger_uuid, reference_id, action_type, amount, public_metadata_json
                FROM points_ledger
                WHERE status='confirmed'
                  AND reference_type='trading_margin_position'
                  AND action_type IN ('trading_margin_collateral_freeze', 'trading_margin_collateral_unfreeze')
                ORDER BY id DESC LIMIT 500
                """
            ).fetchall():
                public_metadata = _json_loads(row["public_metadata_json"], {})
                source = str(public_metadata.get("source_wallet_address") or "").strip().lower()
                if source in normalized and str(row["reference_id"] or ""):
                    margin_refs[str(row["reference_id"])] = {
                        "source_wallet_address": source,
                        "ledger_uuid": row["ledger_uuid"],
                        "action_type": row["action_type"],
                        "amount_points": int(row["amount"] or 0),
                    }
        except Exception:
            margin_refs = {}
        margin_positions = []
        if margin_refs and table_columns(conn, "trading_margin_positions"):
            try:
                ref_placeholders = ", ".join("?" for _ in margin_refs)
                for row in conn.execute(
                    f"""
                    SELECT * FROM trading_margin_positions
                    WHERE position_uuid IN ({ref_placeholders})
                    ORDER BY id DESC LIMIT 100
                    """,
                    tuple(margin_refs.keys()),
                ).fetchall():
                    margin_positions.append({
                        "position_uuid": row["position_uuid"],
                        "user_id": int(row["user_id"] or 0),
                        "market_symbol": row["market_symbol"],
                        "position_type": row["position_type"],
                        "status": row["status"],
                        "collateral_chain_points": int(row["collateral_chain_points"] or 0) if "collateral_chain_points" in row.keys() else 0,
                        "source_trace": margin_refs.get(str(row["position_uuid"] or ""), {}),
                    })
            except Exception:
                margin_positions = []
        bots = []
        if orders and table_columns(conn, "trading_bot_runs") and table_columns(conn, "trading_orders"):
            order_uuids = [item["order_uuid"] for item in orders if item.get("order_uuid")]
            try:
                order_placeholders = ", ".join("?" for _ in order_uuids)
                if order_uuids:
                    bots.extend([
                        {
                            "bot_kind": "trading_bot",
                            "bot_uuid": row["bot_uuid"],
                            "order_uuid": row["order_uuid"],
                            "user_id": int(row["user_id"] or 0),
                            "status": row["status"],
                            "created_at": row["created_at"],
                        }
                        for row in conn.execute(
                            f"""
                            SELECT b.bot_uuid, r.order_uuid, r.user_id, r.status, r.created_at
                            FROM trading_bot_runs r
                            JOIN trading_bots b ON b.id=r.bot_id
                            WHERE r.order_uuid IN ({order_placeholders})
                            ORDER BY r.id DESC LIMIT 100
                            """,
                            tuple(order_uuids),
                        ).fetchall()
                    ])
            except Exception:
                pass
        if orders and table_columns(conn, "trading_grid_orders"):
            order_uuids = [item["order_uuid"] for item in orders if item.get("order_uuid")]
            try:
                order_placeholders = ", ".join("?" for _ in order_uuids)
                if order_uuids:
                    bots.extend([
                        {
                            "bot_kind": "grid_bot",
                            "bot_uuid": row["bot_uuid"],
                            "order_uuid": row["trading_order_uuid"],
                            "grid_order_uuid": row["order_uuid"],
                            "user_id": int(row["user_id"] or 0),
                            "status": row["status"],
                            "created_at": row["created_at"],
                        }
                        for row in conn.execute(
                            f"""
                            SELECT gb.bot_uuid, go.order_uuid, go.trading_order_uuid, go.user_id, go.status, go.created_at
                            FROM trading_grid_orders go
                            JOIN trading_grid_bots gb ON gb.id=go.grid_bot_id
                            WHERE go.trading_order_uuid IN ({order_placeholders})
                            ORDER BY go.id DESC LIMIT 100
                            """,
                            tuple(order_uuids),
                        ).fetchall()
                    ])
            except Exception:
                pass
        return {
            "addresses": normalized,
            "orders": orders,
            "spot_positions": spot_positions,
            "margin_positions": margin_positions,
            "bots": bots,
            "summary": {
                "order_count": len(orders),
                "spot_position_count": len(spot_positions),
                "margin_position_count": len(margin_positions),
                "bot_trace_count": len(bots),
                "lineage_scope": "address_tx_order_position_bot",
            },
        }

    def _seed_recovery_branch_balances_locked(self, conn, *, branch_uuid, parent_branch_uuid, proposal_row, actor=None, override_payload=None):
        payload = override_payload if isinstance(override_payload, dict) else _json_loads(proposal_row["payload_json"], {})
        excluded = set(self._governance_evidence(payload.get("excluded_tx_hashes") or []))
        recovery_strategy = str(payload.get("recovery_strategy") or "treasury_compensation").strip().lower()
        incident_refs = set(self._governance_evidence([proposal_row["incident_tx_hash"], *(payload.get("incident_tx_hashes") or [])]))
        if recovery_strategy == "tainted_remainder_return":
            excluded.difference_update(incident_refs)
        if proposal_row["incident_tx_hash"] and recovery_strategy != "tainted_remainder_return":
            excluded.add(str(proposal_row["incident_tx_hash"]))
        exclude_tainted_descendants = recovery_strategy == "exclude_tainted_descendants"
        replay = self._replay_branch_address_balances_locked(
            conn,
            branch_uuid=parent_branch_uuid or self._main_branch_uuid(),
            excluded_refs=excluded,
            exclude_tainted_descendants=exclude_tainted_descendants,
        )
        replay_balances = dict(replay.get("balances") or {})
        tainted_remainder = {"strategy": recovery_strategy, "return_amount": 0, "items": [], "item_count": 0}
        if recovery_strategy == "tainted_remainder_return":
            tainted_remainder = self._recovery_branch_tainted_remainder_plan_locked(
                conn,
                parent_branch_uuid=parent_branch_uuid or self._main_branch_uuid(),
                incident_tx_hash=proposal_row["incident_tx_hash"],
                incident_tx_hashes=payload.get("incident_tx_hashes") or [],
                replay_balances=replay_balances,
                victim_claims=payload.get("victim_claims") or [],
            )
            for distribution in tainted_remainder.get("distribution") or []:
                source = str(distribution.get("wallet_address") or "").strip().lower()
                amount = int(distribution.get("allocated_points") or 0)
                if amount > 0 and source:
                    replay_balances[source] = int(replay_balances.get(source, 0) or 0) + amount
            for source in tainted_remainder.get("return_sources") or []:
                destination = str(source.get("wallet_address") or "").strip().lower()
                amount = int(source.get("return_amount_points") or 0)
                if amount > 0 and destination:
                    replay_balances[destination] = int(replay_balances.get(destination, 0) or 0) - amount
        compensation_policy = self._recovery_compensation_policy(
            loss_cause=payload.get("loss_cause") or "protocol_fault",
            requested_rate_per_10000=payload.get("compensation_rate_per_10000"),
        )
        compensation = self._recovery_branch_compensation_plan_locked_with_rate(
            conn,
            parent_branch_uuid=parent_branch_uuid or self._main_branch_uuid(),
            excluded_refs=excluded,
            replay_balances=replay_balances,
            compensation_rate_per_10000=compensation_policy["compensation_rate_per_10000"],
        ) if recovery_strategy == "treasury_compensation" else {
            "required_total": 0,
            "gross_shortfall_total": 0,
            "items": [],
            "item_count": 0,
            "strategy": recovery_strategy,
        }
        compensation["policy"] = compensation_policy
        created = []
        skipped = []
        for address, balance in sorted(replay_balances.items()):
            amount = int(balance or 0)
            if amount <= 0:
                if amount < 0:
                    skipped.append({"address": address, "reason": "negative_replay_balance", "balance": amount})
                continue
            if not WALLET_ADDRESS_RE.fullmatch(address):
                skipped.append({"address": address, "reason": "non_wallet_address", "balance": amount})
                continue
            owner = self._wallet_identity_owner_for_address(conn, address)
            if not owner or not owner["user_id"]:
                skipped.append({"address": address, "reason": "unowned_or_inactive_wallet", "balance": amount})
                continue
            ledger, was_created = self._record_transaction(
                conn,
                user_id=int(owner["user_id"]),
                currency_type=DISPLAY_CURRENCY,
                direction="credit",
                amount=amount,
                action_type="recovery_branch_genesis_credit",
                reference_type="recovery_branch",
                reference_id=branch_uuid,
                idempotency_key=f"recovery_branch_genesis:{branch_uuid}:{address}",
                reason="recovery branch genesis balance",
                    public_metadata={
                        "destination_wallet_address": address,
                        "recovery_branch_uuid": branch_uuid,
                        "parent_branch_uuid": parent_branch_uuid,
                        "excluded_tx_hashes": sorted(excluded),
                        "recovery_model": "branch_isolated_asset_universe_v1",
                        "recovery_strategy": recovery_strategy,
                        "tainted_remainder_return": tainted_remainder,
                    },
                actor=actor,
            )
            append_economy_event(
                conn,
                chain_secret=self.chain_secret,
                event_type="recovery_branch_genesis",
                transaction_type="recovery_branch_genesis_credit",
                source_fund_key="mint",
                destination_fund_key=None,
                destination_address=address,
                amount=amount,
                idempotency_key=f"recovery_branch_economy_genesis:{branch_uuid}:{address}",
                metadata={
                    "ledger_uuid": ledger["ledger_uuid"],
                    "recovery_branch_uuid": branch_uuid,
                    "parent_branch_uuid": parent_branch_uuid,
                    "excluded_tx_hashes": sorted(excluded),
                    "recovery_model": "branch_isolated_asset_universe_v1",
                    "recovery_strategy": recovery_strategy,
                    "tainted_remainder_return": tainted_remainder,
                },
                actor=actor,
                chain_branch=branch_uuid,
            )
            created.append({
                "wallet_address": address,
                "user_id": int(owner["user_id"]),
                "amount_points": amount,
                "ledger_uuid": ledger["ledger_uuid"],
                "created": bool(was_created),
            })
        compensation_created = []
        if recovery_strategy == "treasury_compensation":
            economy = replay_economy_events(
                conn,
                chain_secret=self.chain_secret,
                persist_cache=False,
                chain_branch=branch_uuid,
            )
            treasury_balance = int((economy.get("balances") or {}).get("official_treasury", {}).get("balance") or 0)
            if int(compensation.get("required_total") or 0) > treasury_balance:
                raise ValueError("official treasury balance is insufficient for recovery branch compensation")
            total_compensation = int(compensation.get("required_total") or 0)
            if total_compensation:
                append_economy_event(
                    conn,
                    chain_secret=self.chain_secret,
                    event_type="official_treasury_compensation",
                    transaction_type="recovery_branch_treasury_compensation",
                    source_fund_key="official_treasury",
                    destination_fund_key="burn",
                    amount=total_compensation,
                    idempotency_key=f"recovery_branch_treasury_compensation_event:{branch_uuid}",
                    metadata={
                        "recovery_branch_uuid": branch_uuid,
                        "parent_branch_uuid": parent_branch_uuid,
                        "excluded_tx_hashes": sorted(excluded),
                        "recovery_strategy": recovery_strategy,
                        "compensation_plan": compensation,
                        "settlement": "official_treasury_absorbs_negative_gap_created_by_preserving_successor_transactions",
                    },
                    actor=actor,
                    chain_branch=branch_uuid,
                )
                compensation_created.append({
                    "fund_key": "official_treasury",
                    "amount_points": total_compensation,
                    "destination": "burn",
                    "created": True,
                })
        return {
            "parent_branch_uuid": parent_branch_uuid,
            "branch_uuid": branch_uuid,
            "recovery_strategy": recovery_strategy,
            "excluded_refs": sorted(set(replay.get("excluded_refs") or sorted(excluded))),
            "requested_excluded_refs": sorted(excluded),
            "auto_excluded_refs": list(replay.get("auto_excluded_refs") or []),
            "auto_excluded": list(replay.get("auto_excluded") or []),
            "auto_excluded_count": int(replay.get("auto_excluded_count") or 0),
            "exclusion_iterations": int(replay.get("exclusion_iterations") or 0),
            "replayed_ledger_rows": int(replay.get("ledger_rows") or 0),
            "excluded_ledger_rows": int(replay.get("excluded_rows") or 0),
            "created_count": len(created),
            "created": created,
            "compensation": {
                **compensation,
                "created": compensation_created,
                "created_count": len(compensation_created),
            },
            "tainted_remainder_return": tainted_remainder,
            "skipped": skipped[:50],
            "skipped_count": len(skipped),
            "model": "branch_isolated_asset_universe_v1",
        }

    def execute_governance_proposal(self, *, actor, proposal_uuid):
        self._governance_require_executor(actor)
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            self._governance_assert_clock_safe_locked(conn, "execute_governance_proposal")
            row = self._refresh_governance_proposal_locked(conn, proposal_uuid)
            if not row:
                raise ValueError("proposal not found")
            if row["status"] != "passed":
                raise ValueError(f"proposal is {row['status']}; execution requires passed")
            if int(row["root_veto_used"] or 0):
                raise PermissionError("proposal was vetoed by root")
            timelock_until = parse_utc_timestamp(row["timelock_until"])
            if timelock_until and timelock_until > datetime.now(timezone.utc):
                raise ValueError(f"proposal timelock active until {row['timelock_until']}")
            expected_payload_hash = self._governance_execution_payload_hash_for_row(row)
            if row["execution_payload_hash"] != expected_payload_hash:
                raise ValueError("governance execution payload hash mismatch")
            payload = _json_loads(row["payload_json"], {})
            guard_error = self._governance_execution_guard_error(row, payload)
            if guard_error:
                raise ValueError(guard_error)
            multisig_status = self._governance_multisig_status_locked(conn, row)
            if row["governance_domain"] == "OFFICIAL_TREASURY" and not multisig_status.get("ready"):
                raise PermissionError(
                    f"official treasury multisig threshold not reached ({multisig_status.get('signature_count', 0)}/{multisig_status.get('threshold', 0)})"
                )
            now = utc_now()
            action_type = str(row["action_type"] or "").strip().upper()
            if action_type == "MARK_SCAM":
                address = row["target_wallet_address"]
                if not WALLET_ADDRESS_RE.fullmatch(address):
                    raise ValueError("proposal target wallet address invalid")
                risk_level = str(payload.get("risk_level") or "confirmed_scam")
                label = str(payload.get("label") or "governance_confirmed_scam")
                conn.execute(
                    """
                    INSERT INTO points_chain_address_risk_labels (
                        wallet_address, risk_level, status, label, reason, evidence_json,
                        proposal_uuid, created_at, updated_at
                    ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(wallet_address)
                    DO UPDATE SET risk_level=excluded.risk_level, status='active',
                                  label=excluded.label, reason=excluded.reason,
                                  evidence_json=excluded.evidence_json,
                                  proposal_uuid=excluded.proposal_uuid,
                                  updated_at=excluded.updated_at, revoked_at=NULL
                    """,
                    (
                        address,
                        risk_level,
                        label[:80],
                        row["reason"],
                        row["evidence_json"],
                        proposal_uuid,
                        now,
                        now,
                    ),
                )
                result = {
                    "action": "address_risk_label_applied",
                    "wallet_address": address,
                    "risk_label": self._address_risk_label_locked(conn, address),
                }
            elif action_type in {"FREEZE_ADDRESS", "UNFREEZE_ADDRESS"}:
                address = row["target_wallet_address"]
                if not WALLET_ADDRESS_RE.fullmatch(address):
                    raise ValueError("proposal target wallet address invalid")
                release = action_type == "UNFREEZE_ADDRESS"
                if release:
                    conn.execute(
                        """
                        UPDATE points_chain_address_freezes
                        SET status='released', release_proposal_uuid=?, updated_at=?, released_at=?
                        WHERE wallet_address=? AND status='active'
                        """,
                        (proposal_uuid, now, now, address),
                    )
                    conn.execute(
                        """
                        UPDATE points_chain_address_provisional_freezes
                        SET status='released', updated_at=?, released_at=?
                        WHERE wallet_address=? AND status='active'
                        """,
                        (now, now, address),
                    )
                    result = {
                        "action": "wallet_address_unfrozen",
                        "wallet_address": address,
                        "ledger_mutation": "forbidden",
                    }
                else:
                    conn.execute(
                        """
                        INSERT INTO points_chain_address_freezes (
                            wallet_address, status, reason, evidence_json, freeze_proposal_uuid,
                            created_at, updated_at
                        ) VALUES (?, 'active', ?, ?, ?, ?, ?)
                        ON CONFLICT(wallet_address)
                        DO UPDATE SET status='active', reason=excluded.reason,
                                      evidence_json=excluded.evidence_json,
                                      freeze_proposal_uuid=excluded.freeze_proposal_uuid,
                                      release_proposal_uuid=NULL,
                                      updated_at=excluded.updated_at,
                                      released_at=NULL
                        """,
                        (address, row["reason"], row["evidence_json"], proposal_uuid, now, now),
                    )
                    conn.execute(
                        """
                        UPDATE points_chain_address_provisional_freezes
                        SET status='released', updated_at=?, released_at=?
                        WHERE wallet_address=? AND status='active'
                        """,
                        (now, now, address),
                    )
                    result = {
                        "action": "wallet_address_frozen",
                        "wallet_address": address,
                        "freeze": self._address_freeze_locked(conn, address),
                        "freeze_model": "block_outgoing_transfers_only",
                        "ledger_mutation": "forbidden",
                    }
            else:
                payload = _json_loads(row["payload_json"], {})
                if action_type in {"ROLLBACK_BRANCH", "HARD_FORK_ACCEPTANCE"}:
                    result = self._activate_recovery_branch_locked(conn, row=row, proposal_uuid=proposal_uuid, now=now)
                elif action_type in {"TREASURY_TRANSFER", "EXCHANGE_FUND_REPLENISH", "CONTEST_REWARD_PAYOUT"}:
                    transfer = self._official_wallet_grant_locked(
                        conn,
                        actor=actor,
                        destination_wallet_address=payload.get("destination_wallet_address") or row["target_wallet_address"],
                        amount=int(row["requested_amount"] or 0),
                        reason=payload.get("memo") or row["reason"],
                        request_uuid=f"governance:{proposal_uuid}:official_transfer",
                    )
                    result = {
                        "action": "official_treasury_transfer_submitted",
                        "transfer": {
                            "created": bool(transfer.get("created")),
                            "transaction_hash": transfer.get("transaction_hash"),
                            "destination_fund_key": transfer.get("destination_fund_key") or "",
                            "destination_unowned": bool(transfer.get("destination_unowned")),
                            "wallet": None,
                            "transaction": transfer.get("transaction"),
                            "warnings": transfer.get("warnings") or [],
                            "notifications": transfer.get("notifications") or {},
                        },
                    }
                elif action_type == "MINT_REQUEST":
                    destination_fund_key = str(payload.get("destination_fund_key") or "official_treasury").strip().lower()
                    if destination_fund_key not in {"official_treasury", "promo_fund", "exchange_fund"}:
                        raise ValueError("mint destination fund is unsupported")
                    event_branch = payload.get("chain_branch") or self._canonical_branch_uuid(conn)
                    policy = bootstrap_economy_layer(
                        conn,
                        chain_secret=self.chain_secret,
                        actor={"role": "system", "id": None},
                        chain_branch=event_branch,
                    )["policy"]
                    self._mint_request_precheck_locked(
                        conn,
                        amount=int(row["requested_amount"] or 0),
                        reference=row["reference"],
                        policy=policy,
                        chain_branch=event_branch,
                        exclude_proposal_uuid=proposal_uuid,
                        destination_fund_key=destination_fund_key,
                    )
                    event, created = append_economy_event(
                        conn,
                        chain_secret=self.chain_secret,
                        event_type="mint",
                        transaction_type="governance_mint_request",
                        source_fund_key="mint",
                        destination_fund_key=destination_fund_key,
                        amount=int(row["requested_amount"] or 0),
                        idempotency_key=f"governance_mint_ref:{row['reference'] or proposal_uuid}",
                        metadata={"proposal_uuid": proposal_uuid, "reason": row["reason"], "reference": row["reference"]},
                        actor=actor,
                        chain_branch=event_branch,
                    )
                    result = {
                        "action": "mint_executed",
                        "created": bool(created),
                        "event_uuid": event["event_uuid"],
                        "destination_fund_key": destination_fund_key,
                    }
                elif action_type == "EMERGENCY_LOCKDOWN":
                    safe_mode = self._enter_safe_mode(
                        conn,
                        {
                            "ok": False,
                            "governance_lockdown": True,
                            "proposal_uuid": proposal_uuid,
                            "errors": [{"type": "governance_emergency_lockdown", "severity": "critical", "message": row["reason"]}],
                        },
                        "governance_emergency_lockdown",
                    )
                    result = {"action": "incident_lockdown_entered", "safe_mode": safe_mode}
                elif action_type == "AUTO_BURN_POLICY":
                    result = {
                        "action": "auto_burn_policy_approved",
                        "ledger_mutation": "forbidden",
                        "policy_payload": payload,
                    }
                elif action_type == "TREASURY_SIGNER_CHANGE":
                    result = {
                        "action": "treasury_signer_change_approved",
                        "ledger_mutation": "forbidden",
                        "signer_policy_payload": payload,
                        "note": "RC1 records governance approval and multisig consent; signer identity rotation is applied through wallet identity lifecycle controls.",
                    }
                elif action_type == "PARAMETER_CHANGE" and str(payload.get("execution_class") or "").strip().upper() == "MONETARY_POLICY_AMENDMENT":
                    result = self._execute_supply_expansion_request_locked(
                        conn,
                        row=row,
                        payload=payload,
                        proposal_uuid=proposal_uuid,
                        actor=actor,
                        now=now,
                    )
                elif action_type in {"PARAMETER_CHANGE", "FEATURE_ACTIVATION"}:
                    result = {
                        "action": "protocol_policy_approved",
                        "ledger_mutation": "forbidden",
                        "policy_payload": payload,
                    }
                else:
                    raise ValueError("unsupported governance action execution")
            audit = self._append_governance_audit_locked(
                conn,
                proposal_uuid=proposal_uuid,
                event_type="PROPOSAL_EXECUTED",
                actor=actor,
                payload_hash=row["execution_payload_hash"],
                metadata={"result": result, "action_type": action_type, "governance_domain": row["governance_domain"]},
            )
            conn.execute(
                """
                UPDATE points_chain_governance_proposals
                SET status='executed', lifecycle_status='EXECUTED',
                    executed_by=?, executed_at=?, execution_result_json=?,
                    prev_audit_hash=?, audit_hash=?, updated_at=?
                WHERE proposal_uuid=?
                """,
                (
                    int(actor_value(actor, "id")) if actor_value(actor, "id") else None,
                    now,
                    _json_dumps(result),
                    audit["prev_audit_hash"],
                    audit["audit_hash"],
                    now,
                    proposal_uuid,
                ),
            )
            self._audit_log(
                conn,
                "POINTS_CHAIN_GOVERNANCE_PROPOSAL_EXECUTED",
                "critical" if row["governance_domain"] == "EMERGENCY_SECURITY" or action_type in {"ROLLBACK_BRANCH", "HARD_FORK_ACCEPTANCE"} else "warning",
                f"PointsChain governance proposal executed: {action_type}",
                actor=actor,
                metadata={"proposal_uuid": proposal_uuid, "result": result},
            )
            refreshed = conn.execute(
                "SELECT * FROM points_chain_governance_proposals WHERE proposal_uuid=?",
                (proposal_uuid,),
            ).fetchone()
            conn.commit()
            return {"ok": True, "proposal": self._serialize_governance_proposal(conn, refreshed, actor=actor), "result": result}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _serialize_governance_proposal(self, conn, row, *, actor=None):
        if not row:
            return None
        user_vote = None
        actor_id = int(actor_value(actor, "id") or 0)
        if actor_id:
            vote = conn.execute(
                """
                SELECT vote, reason, updated_at FROM points_chain_governance_votes
                WHERE proposal_uuid=? AND voter_user_id=?
                """,
                (row["proposal_uuid"], actor_id),
            ).fetchone()
            if vote:
                user_vote = {"vote": vote["vote"], "reason": vote["reason"], "updated_at": vote["updated_at"]}
        decisive = int(row["yes_count"] or 0) + int(row["no_count"] or 0)
        approval_units = ((int(row["yes_count"] or 0) * 10000) // decisive) if decisive else 0
        differential_units = ((int(row["yes_count"] or 0) - int(row["no_count"] or 0)) * 10000) // max(1, int(row["eligible_voter_count"] or 0))
        timelock_until = parse_utc_timestamp(row["timelock_until"])
        executable = row["status"] == "passed" and not int(row["root_veto_used"] or 0) and (not timelock_until or timelock_until <= datetime.now(timezone.utc))
        multisig_status = self._governance_multisig_status_locked(conn, row, actor=actor) if row["governance_domain"] == "OFFICIAL_TREASURY" else {"required": False, "ready": True}
        payload = _json_loads(row["payload_json"], {})
        guard_error = self._governance_execution_guard_error(row, payload)
        payload_verified = self._governance_execution_payload_hash_for_row(row) == row["execution_payload_hash"] and not guard_error
        payload = self._governance_payload_with_latest_dispute_reply_locked(conn, row, payload)
        votes_cast = int(row["yes_count"] or 0) + int(row["no_count"] or 0) + int(row["abstain_count"] or 0)
        if isinstance(payload.get("official_multisig_policy"), dict):
            payload = {**payload, "official_multisig_policy": multisig_status.get("policy", {})}
        return public_currency_payload({
            "proposal_uuid": row["proposal_uuid"],
            "proposal_id": row["proposal_uuid"],
            "proposal_type": row["proposal_type"],
            "governance_domain": row["governance_domain"],
            "action_type": row["action_type"],
            "lifecycle_status": row["lifecycle_status"],
            "proposal_severity": row["proposal_severity"],
            "sponsor_required": bool(row["sponsor_required"]),
            "sponsor_user_id": row["sponsor_user_id"],
            "sponsor_role": row["sponsor_role"],
            "sponsored_at": row["sponsored_at"],
            "proposal_deposit_points": int(row["proposal_deposit_points"] or 0),
            "proposal_deposit_status": row["proposal_deposit_status"],
            "title": row["title"],
            "description": row["description"],
            "reason": row["reason"],
            "reference": row["reference"],
            "target_wallet_address": row["target_wallet_address"],
            "target_wallet": row["target_wallet_address"],
            "target_address": row["target_address"],
            "target_branch": row["target_branch"],
            "requested_amount": int(row["requested_amount"] or 0),
            "requested_asset": row["requested_asset"],
            "incident_tx_hash": row["incident_tx_hash"],
            "base_block_number": row["base_block_number"],
            "base_block_hash": row["base_block_hash"],
            "payload": payload,
            "evidence": _json_loads(row["evidence_json"], []),
            "evidence_refs": _json_loads(row["evidence_json"], []),
            "impact_scope": row["impact_scope"],
            "risk_summary": row["risk_summary"],
            "opposition_record": row["opposition_record"],
            "eligible_voter_count": int(row["eligible_voter_count"] or 0),
            "quorum_count": int(row["quorum_count"] or 0),
            "quorum_required": int(row["quorum_count"] or 0),
            GOV_QUORUM_RATE_FIELD: int(row[GOV_QUORUM_RATE_FIELD] or 0),
            GOV_PASS_THRESHOLD_RATE_FIELD: int(row[GOV_PASS_THRESHOLD_RATE_FIELD] or 0),
            "yes_threshold": int(row[GOV_YES_THRESHOLD_RATE_FIELD] or row[GOV_PASS_THRESHOLD_RATE_FIELD] or 0),
            GOV_YES_THRESHOLD_RATE_FIELD: int(row[GOV_YES_THRESHOLD_RATE_FIELD] or row[GOV_PASS_THRESHOLD_RATE_FIELD] or 0),
            "vote_differential_required": int(row[GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD] or 0),
            GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD: int(row[GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD] or 0),
            GOV_VOTE_DIFFERENTIAL_RATE_FIELD: differential_units,
            "yes_count": int(row["yes_count"] or 0),
            "no_count": int(row["no_count"] or 0),
            "abstain_count": int(row["abstain_count"] or 0),
            GOV_APPROVAL_RATE_FIELD: approval_units,
            "status": row["status"],
            "voting_starts_at": row["voting_starts_at"],
            "voting_ends_at": row["voting_ends_at"],
            "timelock_until": row["timelock_until"],
            "timelock_ends_at": row["timelock_ends_at"],
            "expires_at": row["expires_at"],
            "root_veto_allowed": bool(row["root_veto_allowed"]),
            "root_veto_used": bool(row["root_veto_used"]),
            "root_vetoed_at": row["root_vetoed_at"],
            "root_veto_reason": row["root_veto_reason"],
            "execution_payload_hash": row["execution_payload_hash"],
            "execution_bundle": {
                "action_type": row["action_type"],
                "governance_domain": row["governance_domain"],
                "target_wallet_address": row["target_wallet_address"],
                "target_address": row["target_address"],
                "target_branch": row["target_branch"],
                "requested_amount": int(row["requested_amount"] or 0),
                "requested_asset": row["requested_asset"],
                "payload_hash": row["execution_payload_hash"],
                "bundle_model": "frozen_execution_bundle_v1",
            },
            "governance_snapshot": {
                "eligible_voter_count": int(row["eligible_voter_count"] or 0),
                "quorum_count": int(row["quorum_count"] or 0),
                GOV_QUORUM_RATE_FIELD: int(row[GOV_QUORUM_RATE_FIELD] or 0),
                GOV_PASS_THRESHOLD_RATE_FIELD: int(row[GOV_PASS_THRESHOLD_RATE_FIELD] or 0),
                GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD: int(row[GOV_VOTE_DIFFERENTIAL_REQUIRED_RATE_FIELD] or 0),
                "snapshot_model": "proposal_creation_voter_and_policy_snapshot_v1",
            },
            "multisig": multisig_status,
            "execution_readiness": {
                "quorum_reached": votes_cast >= int(row["quorum_count"] or 0),
                "vote_succeeded": row["status"] == "passed",
                "timelock_finished": not timelock_until or timelock_until <= datetime.now(timezone.utc),
                "payload_verified": payload_verified,
                "execution_guard_verified": not guard_error,
                "execution_guard_error": guard_error,
                "root_veto_clear": not int(row["root_veto_used"] or 0),
                "multisig_ready": not multisig_status.get("required") or bool(multisig_status.get("ready")),
            },
            "executable": executable and payload_verified and (not multisig_status.get("required") or bool(multisig_status.get("ready"))),
            "execution_result": _json_loads(row["execution_result_json"], {}),
            "audit_hash": row["audit_hash"],
            "prev_audit_hash": row["prev_audit_hash"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "executed_at": row["executed_at"],
            "user_vote": user_vote,
        })

    def list_governance_proposals(self, *, actor=None, limit=50):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            requested_limit = min(100, max(1, int(limit or 50)))
            rows = conn.execute(
                """
                SELECT proposal_uuid
                FROM points_chain_governance_proposals
                ORDER BY id DESC LIMIT ?
                """,
                (max(requested_limit * 5, requested_limit),),
            ).fetchall()
            proposals = []
            actor_id = int(actor_value(actor, "id") or 0)
            for row in rows:
                if actor_id <= 0:
                    continue
                refreshed = self._refresh_governance_proposal_locked(conn, row["proposal_uuid"])
                eligible = set(int(item) for item in _json_loads(refreshed["eligible_voters_json"], [])) if refreshed else set()
                if actor_id not in eligible:
                    continue
                proposals.append(self._serialize_governance_proposal(conn, refreshed, actor=actor))
                if len(proposals) >= requested_limit:
                    break
            conn.commit()
            payload = {"ok": True, "proposals": proposals}
            if self._governance_is_manager_actor(actor):
                addresses = set()
                for proposal in proposals:
                    for key in ("target_wallet_address", "target_address"):
                        addresses.add(str(proposal.get(key) or "").strip().lower())
                    addresses.update(self._collect_wallet_addresses_from_value(proposal.get("payload") or {}))
                labels = self._official_hot_wallet_labels_locked(conn, actor=actor, addresses=addresses)
                if labels:
                    payload["official_hot_wallet_labels"] = labels
            return payload
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _official_treasury_flow_label(self, transaction_type):
        labels = {
            "treasury_allocation": "創世 / Mint 發行撥補（非營收）",
            "mint_request": "治理 Mint 發行撥補（非營收）",
            "governance_mint_request": "治理 Mint 發行撥補（非營收）",
            "wallet_creation_fee": "錢包建立服務費",
            "service_fee_batch_debit": "舊版站內服務費批次結算",
            "video_tip_credit": "官方影片投幣收入",
            "video_tip_platform_fee": "影音投幣平台抽成",
            "official_wallet_grant": "官方錢包對外撥款",
            "official_fund_transfer": "官方基金調度",
            "recovery_branch_official_treasury_carry_forward": "分支官方基金承接",
        }
        value = str(transaction_type or "")
        if value.startswith("service_fee_internal_debit:"):
            return "站內服務費即時結算"
        return labels.get(value, value.replace("_", " ").strip().title() or "未分類")

    def _official_treasury_service_fee_price_fit_locked(self, conn):
        catalog_rows = conn.execute(
            """
            SELECT item_key, item_name, category, base_price, min_price, max_price, enabled, metadata_json
            FROM economy_price_catalog
            ORDER BY category, item_key
            """
        ).fetchall()
        catalog_by_key = {str(row["item_key"]): row for row in catalog_rows}
        recommended = [
            {
                "item_key": "post_cost_standard",
                "item_name": "一般發文成本",
                "category": "forum",
                "recommended_points": 1,
                "min_price": 1,
                "max_price": 10,
                "rationale": "低額防洗版，低於每日登入 5 點，不阻礙一般互動。",
            },
            {
                "item_key": "post_pin_24h",
                "item_name": "文章置頂 24 小時",
                "category": "forum",
                "recommended_points": 100,
                "min_price": 50,
                "max_price": 300,
                "rationale": "屬於曝光型功能，價格約等於 20 天每日登入。",
            },
            {
                "item_key": "cloud_storage_1gb_7d",
                "item_name": "雲端容量 1GB / 7 天",
                "category": "cloud_drive",
                "recommended_points": 100,
                "min_price": 50,
                "max_price": 500,
                "metadata": {"storage_bytes": 1024 ** 3, "duration_days": 7, "label": "雲端容量 1GB / 7 天"},
                "rationale": "容量是持續成本，保留比互動類更高的 sink。",
            },
            {
                "item_key": "cloud_storage_1gb_30d",
                "item_name": "雲端容量 1GB / 30 天",
                "category": "cloud_drive",
                "recommended_points": 400,
                "min_price": 200,
                "max_price": 2000,
                "metadata": {"storage_bytes": 1024 ** 3, "duration_days": 30, "label": "雲端容量 1GB / 30 天"},
                "rationale": "30 天方案是 7 天方案 4 倍點數，換取較長有效期。",
            },
            {
                "item_key": "comfyui_txt2img_basic",
                "item_name": "基礎生圖一次",
                "category": "comfyui",
                "recommended_points": 5,
                "min_price": 1,
                "max_price": 25,
                "rationale": "等同每日登入一次，適合低門檻試用。",
            },
            {
                "item_key": "comfyui_txt2img_highres",
                "item_name": "高解析生圖一次",
                "category": "comfyui",
                "recommended_points": 12,
                "min_price": 5,
                "max_price": 60,
                "rationale": "較高資源消耗，約基礎生圖 2-3 倍。",
            },
            {
                "item_key": "video_publish_basic",
                "item_name": "影音發布處理費",
                "category": "video",
                "recommended_points": 2,
                "min_price": 1,
                "max_price": 20,
                "rationale": "發布低價，主要收入仍來自投幣抽成與流量分潤反向激勵。",
            },
            {
                "item_key": "video_boost_24h",
                "item_name": "影音曝光加成 24 小時",
                "category": "video",
                "recommended_points": 80,
                "min_price": 30,
                "max_price": 300,
                "rationale": "曝光型功能需高於一般發布，避免洗推薦。",
            },
            {
                "item_key": "game_entry_standard",
                "item_name": "遊戲一般入場",
                "category": "game",
                "recommended_points": 1,
                "min_price": 1,
                "max_price": 10,
                "rationale": "高頻低額，採 pc0 站內帳本即時扣款，不逐筆等待鏈上確認。",
            },
            {
                "item_key": "marketplace_listing_fee",
                "item_name": "市集上架費",
                "category": "marketplace",
                "recommended_points": 3,
                "min_price": 1,
                "max_price": 30,
                "rationale": "低額抑制垃圾上架，成交抽成可另走平台收入。",
            },
            {
                "item_key": "ai_agent_task_basic",
                "item_name": "AI Agent 基礎任務",
                "category": "ai_task",
                "recommended_points": 10,
                "min_price": 5,
                "max_price": 100,
                "rationale": "比生圖高，預留外部 API / 任務排程成本。",
            },
            {
                "item_key": "username_change",
                "item_name": "改名",
                "category": "account",
                "recommended_points": 200,
                "min_price": 100,
                "max_price": 1000,
                "rationale": "低頻身分操作，價格維持較高以降低濫用。",
            },
            {
                "item_key": "wallet_creation_fee",
                "item_name": "第二個以上錢包建立費",
                "category": "account",
                "recommended_points": WALLET_CREATION_FEE_BASE_POINTS,
                "min_price": WALLET_CREATION_FEE_BASE_POINTS,
                "max_price": WALLET_CREATION_FEE_MAX_POINTS,
                "rationale": "第一個免費，後續依指數提高，所得入官方 Treasury。",
                "policy": {
                    "first_wallet_free": True,
                    "base_points": WALLET_CREATION_FEE_BASE_POINTS,
                    "multiplier": WALLET_CREATION_FEE_MULTIPLIER,
                    "max_points": WALLET_CREATION_FEE_MAX_POINTS,
                },
            },
        ]
        for item in recommended:
            current = catalog_by_key.get(item["item_key"])
            item["current_points"] = int(current["base_price"] or 0) if current else None
            item["enabled"] = bool(int(current["enabled"] or 0)) if current else False
            item["delta_points"] = None if current is None else int(item["recommended_points"]) - int(current["base_price"] or 0)
            if current and not item.get("metadata"):
                item["metadata"] = _json_loads(current["metadata_json"], {})
        return recommended

    def _official_treasury_income_expense_analysis_locked(self, conn, *, branch, official_wallet=None, limit=25):
        branch = str(branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        limit = min(100, max(1, int(limit or 25)))
        now_dt = datetime.now(timezone.utc).replace(microsecond=0)
        period_start_dt = now_dt.replace(day=1, hour=0, minute=0, second=0)
        period_start = period_start_dt.isoformat().replace("+00:00", "Z")
        period_end = now_dt.isoformat().replace("+00:00", "Z")
        period_label = period_start_dt.strftime("%Y-%m")
        service_rows = conn.execute(
            """
            SELECT c.item_key, COALESCE(p.item_name, c.item_key) AS item_name, c.status,
                   COUNT(*) AS count, COALESCE(SUM(c.amount_points), 0) AS amount,
                   MIN(c.created_at) AS first_created_at, MAX(c.created_at) AS last_created_at,
                   MAX(c.settled_at) AS last_settled_at
            FROM points_service_fee_charges c
            LEFT JOIN economy_price_catalog p ON p.item_key=c.item_key
            WHERE c.chain_branch=?
              AND COALESCE(c.settled_at, c.created_at) >= ?
            GROUP BY c.item_key, c.status
            ORDER BY amount DESC, c.item_key
            """,
            (branch, period_start),
        ).fetchall()
        service_by_item = {}
        service_status_totals = {"reserved": 0, "settled": 0, "cancelled": 0}
        for row in service_rows:
            key = str(row["item_key"] or "")
            item = service_by_item.setdefault(
                key,
                {
                    "item_key": key,
                    "item_name": row["item_name"] or key,
                    "reserved_points": 0,
                    "settled_points": 0,
                    "cancelled_points": 0,
                    "charge_count": 0,
                    "last_activity_at": "",
                },
            )
            status = str(row["status"] or "")
            amount = int(row["amount"] or 0)
            count = int(row["count"] or 0)
            if status in service_status_totals:
                service_status_totals[status] += amount
                item[f"{status}_points"] += amount
            item["charge_count"] += count
            item["last_activity_at"] = row["last_settled_at"] or row["last_created_at"] or item["last_activity_at"]

        settlement_rows = conn.execute(
            """
            SELECT ledger_uuid, ledger_hash, amount, action_type, created_at, public_metadata_json
            FROM points_ledger
            WHERE chain_branch=?
              AND (action_type='service_fee_batch_debit' OR action_type LIKE 'service_fee_internal_debit:%')
              AND created_at >= ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (branch, period_start, limit),
        ).fetchall()
        recent_settlements = []
        for row in settlement_rows:
            metadata = _json_loads(row["public_metadata_json"], {})
            recent_settlements.append({
                "ledger_uuid": row["ledger_uuid"],
                "ledger_hash": row["ledger_hash"],
                "amount_points": int(row["amount"] or 0),
                "action_type": row["action_type"],
                "created_at": row["created_at"],
                "batch_uuid": metadata.get("batch_uuid") or "",
                "charge_count": int(metadata.get("charge_count") or 0),
                "destination_fund_key": SERVICE_FEE_REVENUE_DESTINATION_FUND,
                "destination_label": "官方 Treasury",
            })

        event_rows = conn.execute(
            """
            SELECT event_uuid, event_type, transaction_type, source_fund_key, source_address,
                   destination_fund_key, destination_address, amount, metadata_json, created_at
            FROM points_economy_events
            WHERE status='confirmed'
              AND chain_branch=?
              AND (source_fund_key='official_treasury' OR destination_fund_key='official_treasury')
              AND created_at >= ?
            ORDER BY id DESC
            """,
            (branch, period_start),
        ).fetchall()
        income_by_type = {}
        expense_by_type = {}
        non_operating_mint_by_type = {}
        recent_income = []
        recent_expense = []
        recent_non_operating_mint = []
        for row in event_rows:
            source = str(row["source_fund_key"] or "")
            destination = str(row["destination_fund_key"] or "")
            amount = int(row["amount"] or 0)
            transaction_type = str(row["transaction_type"] or "")
            bucket = None
            recent_target = None
            non_operating_mint = source == NON_OPERATING_MINT_SOURCE_FUND
            if destination == "official_treasury" and source != "official_treasury" and non_operating_mint:
                bucket = non_operating_mint_by_type
                recent_target = recent_non_operating_mint
            elif destination == "official_treasury" and source != "official_treasury":
                bucket = income_by_type
                recent_target = recent_income
            elif source == "official_treasury" and destination != "official_treasury":
                bucket = expense_by_type
                recent_target = recent_expense
            if bucket is None:
                continue
            current = bucket.setdefault(
                transaction_type,
                {
                    "transaction_type": transaction_type,
                    "label": self._official_treasury_flow_label(transaction_type),
                    "amount_points": 0,
                    "count": 0,
                    "latest_at": "",
                },
            )
            current["amount_points"] += amount
            current["count"] += 1
            current["latest_at"] = current["latest_at"] or row["created_at"]
            if len(recent_target) < limit:
                recent_target.append({
                    "event_uuid": row["event_uuid"],
                    "transaction_type": transaction_type,
                    "label": self._official_treasury_flow_label(transaction_type),
                    "amount_points": amount,
                    "source_fund_key": source,
                    "source_address": row["source_address"] or "",
                    "destination_fund_key": destination,
                    "destination_address": row["destination_address"] or "",
                    "created_at": row["created_at"],
                    "metadata": _json_loads(row["metadata_json"], {}),
                })

        video_fee_row = conn.execute(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS amount, MAX(created_at) AS latest_at
            FROM points_ledger
            WHERE chain_branch=? AND action_type='video_tip_platform_fee' AND status='confirmed'
              AND created_at >= ?
            """,
            (branch, period_start),
        ).fetchone()
        video_fee_total = int((video_fee_row["amount"] if video_fee_row else 0) or 0)
        if video_fee_total and "video_tip_platform_fee" not in income_by_type:
            income_by_type["video_tip_platform_fee"] = {
                "transaction_type": "video_tip_platform_fee",
                "label": self._official_treasury_flow_label("video_tip_platform_fee"),
                "amount_points": video_fee_total,
                "count": int((video_fee_row["count"] if video_fee_row else 0) or 0),
                "latest_at": (video_fee_row["latest_at"] if video_fee_row else "") or "",
                "ledger_only": True,
                "note": "歷史投幣抽成可能仍在 legacy product ledger；新事件會 walletize 到官方 Treasury fund ledger。",
            }

        income_categories = sorted(income_by_type.values(), key=lambda item: (-int(item["amount_points"]), item["transaction_type"]))
        expense_categories = sorted(expense_by_type.values(), key=lambda item: (-int(item["amount_points"]), item["transaction_type"]))
        non_operating_mint_categories = sorted(non_operating_mint_by_type.values(), key=lambda item: (-int(item["amount_points"]), item["transaction_type"]))
        income_total = sum(int(item["amount_points"]) for item in income_categories)
        expense_total = sum(int(item["amount_points"]) for item in expense_categories)
        non_operating_mint_total = sum(int(item["amount_points"]) for item in non_operating_mint_categories)
        legacy_reserved_total = int(service_status_totals["reserved"])
        service_fee_revenue_total = int(service_status_totals["settled"])
        official_balance = int((official_wallet or {}).get("balance") or 0)
        flow_categories = []
        for item in income_categories:
            flow_categories.append({
                "category_key": str(item.get("transaction_type") or ""),
                "label": item.get("label") or item.get("transaction_type") or "收入",
                "direction": "inflow",
                "inflow_points": int(item.get("amount_points") or 0),
                "outflow_points": 0,
                "net_points": int(item.get("amount_points") or 0),
                "event_count": int(item.get("count") or 0),
                "latest_at": item.get("latest_at") or "",
                "ledger_only": bool(item.get("ledger_only")),
            })
        for item in expense_categories:
            amount = int(item.get("amount_points") or 0)
            flow_categories.append({
                "category_key": str(item.get("transaction_type") or ""),
                "label": item.get("label") or item.get("transaction_type") or "支出",
                "direction": "outflow",
                "inflow_points": 0,
                "outflow_points": amount,
                "net_points": -amount,
                "event_count": int(item.get("count") or 0),
                "latest_at": item.get("latest_at") or "",
                "ledger_only": bool(item.get("ledger_only")),
            })
        flow_categories.sort(key=lambda item: (-abs(int(item["net_points"])), str(item["label"])))
        service_fee_flow_categories = []
        for item in service_by_item.values():
            settled = int(item.get("settled_points") or 0)
            cancelled = int(item.get("cancelled_points") or 0)
            reserved = int(item.get("reserved_points") or 0)
            service_fee_flow_categories.append({
                "category_key": item.get("item_key") or "",
                "label": item.get("item_name") or item.get("item_key") or "服務費",
                "direction": "inflow" if settled else ("pending" if reserved else "flat"),
                "inflow_points": settled,
                "outflow_points": 0,
                "net_points": settled,
                "reserved_points": reserved,
                "cancelled_points": cancelled,
                "event_count": int(item.get("charge_count") or 0),
                "latest_at": item.get("last_activity_at") or "",
            })
        service_fee_flow_categories.sort(key=lambda item: (-int(item["inflow_points"]) - int(item["reserved_points"]), str(item["label"])))
        if official_balance <= 0 and expense_total > 0:
            balance_status = "red"
            balance_reason = "official_treasury_empty"
        elif expense_total > income_total and official_balance < expense_total:
            balance_status = "yellow"
            balance_reason = "expenses_exceed_visible_income"
        else:
            balance_status = "green"
            balance_reason = "visible_income_expense_balanced"
        planned_expenses = [
            {"key": "manager_salary", "label": "管理員薪水", "funding_source": "official_treasury", "governance_required": True},
            {"key": "exchange_fund_replenish", "label": "交易所基金補助", "funding_source": "official_treasury", "governance_required": True},
            {"key": "contest_reward_payout", "label": "競賽獎金", "funding_source": "official_treasury_or_promo", "governance_required": True},
            {"key": "game_weekly_reward", "label": "遊戲區每週獎金", "funding_source": "promo_fund_or_official_treasury", "governance_required": True},
            {"key": "contribution_reward", "label": "貢獻度獎金", "funding_source": "promo_fund_or_official_treasury", "governance_required": True},
            {"key": "video_traffic_reward", "label": "影音流量獎金", "funding_source": "promo_fund_or_official_treasury", "governance_required": True},
            {"key": "forum_traffic_reward", "label": "討論區流量獎金", "funding_source": "promo_fund_or_official_treasury", "governance_required": True},
            {"key": "security_bounty", "label": "資安賞金", "funding_source": "official_treasury", "governance_required": True},
            {"key": "recovery_compensation", "label": "治理補償 / 事故賠付", "funding_source": "official_treasury_or_insurance_policy", "governance_required": True},
            {"key": "infrastructure_budget", "label": "伺服器 / 模型 / 儲存營運成本", "funding_source": "official_treasury", "governance_required": True},
        ]
        return {
            "chain_branch": branch,
            "generated_at": utc_now(),
            "period": {
                "kind": "calendar_month",
                "label": period_label,
                "start_at": period_start,
                "end_at": period_end,
            },
            "status": balance_status,
            "reason": balance_reason,
            "summary": {
                "official_wallet_balance_points": official_balance,
                "income_total_points": income_total,
                "expense_total_points": expense_total,
                "net_points": income_total - expense_total,
                "service_fee_revenue_points": service_fee_revenue_total,
                "legacy_reserved_service_fee_points": legacy_reserved_total,
                "non_operating_mint_allocation_points": non_operating_mint_total,
            },
            "flow_summary": {
                "total_inflow_points": income_total,
                "total_outflow_points": expense_total,
                "net_flow_points": income_total - expense_total,
                "current_balance_points": official_balance,
                "status": balance_status,
                "reason": balance_reason,
                "event_count": sum(int(item.get("event_count") or 0) for item in flow_categories),
                "category_count": len(flow_categories),
                "categories": flow_categories,
            },
            "settlement_policy": {
                "service_fee_layer": "pc0_internal_immediate_debit",
                "service_fee_ledger_action": "service_fee_internal_debit",
                "service_fee_destination_fund_key": SERVICE_FEE_REVENUE_DESTINATION_FUND,
                "chain_transaction_fee_destination_fund_key": "burn",
                "video_tip_commission_destination_fund_key": "official_treasury",
                "note": "pc0 站內託管錢包服務費即時進官方 Treasury；鏈上交易 fee / 加速 fee 仍進 BURN。冷錢包直接服務付款已停用，待正式 cold-chain approval rail。",
            },
            "service_fee_items": sorted(service_by_item.values(), key=lambda item: (-int(item["settled_points"] + item["reserved_points"]), item["item_key"])),
            "service_fee_flow_categories": service_fee_flow_categories,
            "recent_service_fee_settlements": recent_settlements,
            "recent_service_fee_revenue_ledgers": recent_settlements,
            "income_categories": income_categories,
            "expense_categories": expense_categories,
            "non_operating_mint_categories": non_operating_mint_categories,
            "recent_income_events": recent_income,
            "recent_expense_events": recent_expense,
            "recent_non_operating_mint_events": recent_non_operating_mint,
            "planned_expense_categories": planned_expenses,
            "perspectives": {
                "user": {
                    "status": "balanced" if balance_status != "red" else "watch",
                    "summary": "站內服務預設使用 pc0 站內託管錢包即時扣款，不等待 20 proved；鏈上轉帳 fee 仍是 burn，不會變成 root 收益。",
                    "risks": [
                        "高頻服務費若定價過高會抑制互動。",
                        "自管冷錢包付款仍需本機簽章，不能靜默扣款。",
                    ],
                },
                "manager": {
                    "status": balance_status,
                    "summary": "官方 Treasury 的可見收入需覆蓋營運支出、獎金、補償與交易所基金撥補；支出仍需治理與官方多簽。",
                    "risks": [
                        "若長期支出大於服務收入與投幣抽成，應先調整價格、降低補貼或提案撥補，不應直接 mint。",
                        "服務費是營運收入；鏈上 fee burn 是供給 sink，兩者不可混用。",
                    ],
                },
            },
        }

    def official_treasury_signer_center(self, *, actor=None, limit=50):
        if not self._governance_is_manager_actor(actor):
            raise PermissionError("manager+ required to view official treasury signer center")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._governance_begin_immediate(conn)
            branch = self._canonical_branch_uuid(conn)
            circulation = self._official_hot_wallet_circulation_with_economy_breakdown_locked(
                conn,
                branch_uuid=branch,
                actor=actor,
            )
            layer = economy_layer_report(
                conn,
                chain_secret=self.chain_secret,
                actor=actor,
                circulation=circulation,
                chain_branch=branch,
            )
            funds = layer.get("funds") or {}
            fund_addresses = {
                key: str((dict(value) if value and hasattr(value, "keys") else value or {}).get("address") or "")
                for key, value in funds.items()
                if key in {"mint", "official_treasury", "promo_fund", "exchange_fund", "burn"}
            }
            official_wallet = dict(funds.get("official_treasury") or {})
            official_wallet.update({
                "wallet_type": "official_treasury_multisig",
                "wallet_scope": "official_treasury",
                "custody_mode": "multisig",
                "spend_capability": "enabled",
                "control_model": "governance_vote_then_official_multisig_threshold_v1",
            })
            policy_error = ""
            try:
                policy = self._governance_multisig_policy_locked(conn, fund_key="official_treasury")
            except Exception as exc:
                policy = {
                    "policy_version": "OFFICIAL_TREASURY_MULTISIG_V1",
                    "fund_key": "official_treasury",
                    "threshold": 0,
                    "threshold_weight": 0,
                    "signer_count": 0,
                    "total_weight": 0,
                    "signers": [],
                    "signer_addresses": [],
                    "signature_required_after_governance": True,
                    "wallet_type": "official_treasury_multisig",
                    "wallet_scope": "official_treasury",
                }
                policy_error = str(exc)
            rows = conn.execute(
                """
                SELECT proposal_uuid
                FROM points_chain_governance_proposals
                WHERE governance_domain='OFFICIAL_TREASURY'
                  AND status NOT IN ('executed', 'cancelled', 'expired')
                ORDER BY id DESC
                LIMIT ?
                """,
                (min(100, max(1, int(limit or 50))),),
            ).fetchall()
            proposals = []
            signable = []
            for row in rows:
                refreshed = self._refresh_governance_proposal_locked(conn, row["proposal_uuid"])
                payload = self._serialize_governance_proposal(conn, refreshed, actor=actor)
                proposals.append(payload)
                multisig = payload.get("multisig") if isinstance(payload.get("multisig"), dict) else {}
                if multisig.get("required") and not multisig.get("ready") and payload.get("status") == "passed":
                    signer_address = (multisig.get("policy") or {}).get("current_signer_wallet_address") or ""
                    signing_payload = self._governance_multisig_signing_payload(refreshed)
                    signable.append({
                        "proposal_uuid": payload.get("proposal_uuid") or "",
                        "action_type": payload.get("action_type") or "",
                        "target_wallet_address": payload.get("target_wallet_address") or "",
                        "requested_amount": int(payload.get("requested_amount") or 0),
                        "timelock_until": payload.get("timelock_until") or "",
                        "execution_payload_hash": payload.get("execution_payload_hash") or "",
                        "signing_payload": signing_payload,
                        "signing_payload_hash": sha256_text(canonical_json(signing_payload)),
                        "offline_verifier_model": "canonical_json_sha256_v1",
                        "current_signer_wallet_address": signer_address,
                        "signature_count": int(multisig.get("signature_count") or 0),
                        "threshold": int(multisig.get("threshold") or 0),
                        "signature_weight": int(multisig.get("signature_weight") or 0),
                        "threshold_weight": int(multisig.get("threshold_weight") or 0),
                        "readiness": payload.get("execution_readiness") or {},
                    })
            conn.commit()
            return {
                "ok": True,
                "official_wallet": official_wallet,
                "economy_layer": layer,
                "fund_addresses": fund_addresses,
                "policy": policy,
                "policy_error": policy_error,
                "pending_proposals": proposals,
                "signable": signable,
                "treasury_analysis": self._official_treasury_income_expense_analysis_locked(
                    conn,
                    branch=branch,
                    official_wallet=official_wallet,
                    limit=limit,
                ),
                "canonical_branch": branch,
                "rc1_scope": "official_treasury_multisig_only",
                "user_multisig_policy": "receive_only_preview_no_transfer",
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _governance_audit_verify_locked(self, conn, limit=None):
        sql = "SELECT * FROM points_chain_governance_audit_log ORDER BY id ASC"
        rows = conn.execute(sql).fetchall()
        errors = []
        previous = ""
        for row in rows:
            metadata = _json_loads(row["metadata_json"], {})
            payload = {
                "audit_uuid": row["audit_uuid"],
                "proposal_uuid": row["proposal_uuid"],
                "event_type": row["event_type"],
                "actor_user_id": row["actor_user_id"],
                "actor_role": row["actor_role"],
                "payload_hash": row["payload_hash"],
                "metadata": metadata,
                "prev_audit_hash": row["prev_audit_hash"],
                "created_at": row["created_at"],
            }
            expected = sha256_text(canonical_json(payload))
            if row["prev_audit_hash"] != previous:
                errors.append({"type": "governance_audit_previous_hash", "audit_uuid": row["audit_uuid"], "expected": previous, "actual": row["prev_audit_hash"]})
            if row["audit_hash"] != expected:
                errors.append({"type": "governance_audit_hash", "audit_uuid": row["audit_uuid"], "expected": expected, "actual": row["audit_hash"]})
            previous = row["audit_hash"]
            if limit and len(errors) >= int(limit):
                break
        return {"ok": not errors, "event_count": len(rows), "error_count": len(errors), "errors": errors[:100], "head_hash": previous}

    def verify_governance_audit(self):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            return self._governance_audit_verify_locked(conn)
        finally:
            conn.close()

    def _governance_branch_tree_locked(self, conn, branch_rows):
        rows = list(branch_rows or [])
        active_branch = self._canonical_branch_uuid(conn)
        node_map = {}
        for row in rows:
            replay_plan = _json_loads(row["replay_plan_json"], {})
            branch_uuid = str(row["branch_uuid"] or self._main_branch_uuid())
            node_map[branch_uuid] = {
                "branch_uuid": branch_uuid,
                "proposal_uuid": row["proposal_uuid"],
                "parent_branch_uuid": row["parent_branch_uuid"],
                "branch_name": row["branch_name"],
                "base_block_number": row["base_block_number"],
                "base_block_hash": row["base_block_hash"],
                "incident_tx_hash": row["incident_tx_hash"],
                "status": row["status"],
                "is_canonical": bool(row["is_canonical"]),
                "write_enabled": bool(row["write_enabled"]) if "write_enabled" in row.keys() else bool(row["is_canonical"]),
                "recovery_type": row["recovery_type"] if "recovery_type" in row.keys() else "canonical_pointer_only",
                "replay_plan": replay_plan,
                "created_at": row["created_at"],
                "activated_at": row["activated_at"],
            }
        if self._main_branch_uuid() not in node_map:
            main_meta = self._branch_metadata(conn, self._main_branch_uuid())
            node_map[self._main_branch_uuid()] = {
                "branch_uuid": self._main_branch_uuid(),
                "proposal_uuid": "",
                "parent_branch_uuid": "",
                "branch_name": "main",
                "base_block_number": None,
                "base_block_hash": "",
                "incident_tx_hash": "",
                "status": main_meta.get("status") or ("canonical_main" if active_branch == self._main_branch_uuid() else "read_only_archived"),
                "is_canonical": active_branch == self._main_branch_uuid(),
                "write_enabled": bool(main_meta.get("write_enabled", active_branch == self._main_branch_uuid())),
                "recovery_type": main_meta.get("recovery_type") or "canonical_pointer_only",
                "replay_plan": {"asset_universe": "main"},
                "created_at": "",
                "activated_at": "",
            }

        ledger_stats = {
            row["chain_branch"]: dict(row)
            for row in conn.execute(
                """
                SELECT chain_branch,
                       COUNT(*) AS ledger_count,
                       COALESCE(SUM(CASE WHEN chain_block_id IS NULL THEN 1 ELSE 0 END), 0) AS unsealed_ledger_count,
                       COUNT(DISTINCT chain_block_id) AS sealed_block_count,
                       MIN(created_at) AS first_ledger_at,
                       MAX(created_at) AS latest_ledger_at
                FROM points_ledger
                WHERE status='confirmed'
                GROUP BY chain_branch
                """
            ).fetchall()
        }
        economy_stats = {
            row["chain_branch"]: dict(row)
            for row in conn.execute(
                """
                SELECT chain_branch,
                       COUNT(*) AS economy_event_count,
                       MIN(created_at) AS first_economy_event_at,
                       MAX(created_at) AS latest_economy_event_at
                FROM points_economy_events
                WHERE status='confirmed'
                GROUP BY chain_branch
                """
            ).fetchall()
        }
        child_map = {branch_uuid: [] for branch_uuid in node_map}
        for branch_uuid, node in node_map.items():
            parent = str(node.get("parent_branch_uuid") or "")
            if parent and parent in child_map and branch_uuid != parent:
                child_map[parent].append(branch_uuid)

        def depth_for(branch_uuid, seen=None):
            seen = set(seen or set())
            if branch_uuid in seen:
                return 0
            seen.add(branch_uuid)
            parent = str((node_map.get(branch_uuid) or {}).get("parent_branch_uuid") or "")
            if not parent or parent not in node_map:
                return 0
            return 1 + depth_for(parent, seen)

        nodes = []
        for branch_uuid, node in node_map.items():
            ledger = ledger_stats.get(branch_uuid) or {}
            economy = economy_stats.get(branch_uuid) or {}
            replay_plan = node.get("replay_plan") if isinstance(node.get("replay_plan"), dict) else {}
            status = str(node.get("status") or "")
            is_canonical = branch_uuid == active_branch or bool(node.get("is_canonical"))
            write_enabled = bool(node.get("write_enabled"))
            has_incident = bool(node.get("incident_tx_hash") or node.get("proposal_uuid") or replay_plan.get("selected_recovery_strategy") or replay_plan.get("recovery_strategy"))
            archived = not is_canonical and not write_enabled
            node_state = "canonical_write" if is_canonical and write_enabled else ("canonical_read_only" if is_canonical else ("archived" if archived else status or "branch"))
            children = sorted(child_map.get(branch_uuid) or [])
            nodes.append({
                **node,
                "is_canonical": is_canonical,
                "write_enabled": write_enabled,
                "depth": depth_for(branch_uuid),
                "node_state": node_state,
                "child_branch_uuids": children,
                "children_count": len(children),
                "ledger_count": int(ledger.get("ledger_count") or 0),
                "unsealed_ledger_count": int(ledger.get("unsealed_ledger_count") or 0),
                "sealed_block_count": int(ledger.get("sealed_block_count") or 0),
                "first_ledger_at": ledger.get("first_ledger_at") or "",
                "latest_ledger_at": ledger.get("latest_ledger_at") or "",
                "economy_event_count": int(economy.get("economy_event_count") or 0),
                "first_economy_event_at": economy.get("first_economy_event_at") or "",
                "latest_economy_event_at": economy.get("latest_economy_event_at") or "",
                "has_incident": has_incident,
                "auto_collapsed": bool(archived and not is_canonical),
            })
        nodes.sort(key=lambda item: (int(item.get("depth") or 0), item.get("created_at") or "", item["branch_uuid"]))
        return {
            "canonical_branch_uuid": active_branch,
            "root_branch_uuid": self._main_branch_uuid(),
            "node_count": len(nodes),
            "archived_count": sum(1 for item in nodes if not item.get("is_canonical")),
            "write_enabled_count": sum(1 for item in nodes if item.get("write_enabled")),
            "nodes": nodes,
        }

    def _governance_report_locked(self, conn):
        proposals = conn.execute(
            """
            SELECT * FROM points_chain_governance_proposals
            ORDER BY id DESC LIMIT 10
            """
        ).fetchall()
        labels = conn.execute(
            """
            SELECT * FROM points_chain_address_risk_labels
            WHERE status='active'
            ORDER BY updated_at DESC LIMIT 20
            """
        ).fetchall()
        freezes = conn.execute(
            """
            SELECT * FROM points_chain_address_freezes
            WHERE status='active'
            ORDER BY updated_at DESC LIMIT 20
            """
        ).fetchall()
        provisional_freezes = conn.execute(
            """
            SELECT * FROM points_chain_address_provisional_freezes
            WHERE status='active'
            ORDER BY updated_at DESC LIMIT 20
            """
        ).fetchall()
        branch_rows = conn.execute(
            """
            SELECT * FROM points_chain_governance_branches
            ORDER BY id ASC LIMIT 100
            """
        ).fetchall()
        branches = list(reversed(branch_rows[-10:]))
        audit_rows = conn.execute(
            """
            SELECT * FROM points_chain_governance_audit_log
            ORDER BY id DESC LIMIT 20
            """
        ).fetchall()
        active_risk_labels = [
            item
            for item in (self._address_risk_label_locked(conn, row["wallet_address"]) for row in labels)
            if item
        ]
        active_freezes = [
            item
            for item in (self._address_freeze_locked(conn, row["wallet_address"]) for row in freezes)
            if item
        ]
        active_provisional_freezes = []
        expired_provisional_freezes = 0
        for row in provisional_freezes:
            item = self._address_provisional_freeze_locked(conn, row["wallet_address"])
            if item:
                active_provisional_freezes.append(item)
            else:
                expired_provisional_freezes += 1
        if expired_provisional_freezes:
            conn.commit()
        return {
            "proposals": [self._serialize_governance_proposal(conn, row) for row in proposals],
            "active_risk_labels": active_risk_labels,
            "active_freezes": active_freezes,
            "active_provisional_freezes": active_provisional_freezes,
            "audit_verify": self._governance_audit_verify_locked(conn),
            "audit_events": [
                {
                    "audit_uuid": row["audit_uuid"],
                    "proposal_uuid": row["proposal_uuid"],
                    "event_type": row["event_type"],
                    "actor_user_id": row["actor_user_id"],
                    "actor_role": row["actor_role"],
                    "payload_hash": row["payload_hash"],
                    "prev_audit_hash": row["prev_audit_hash"],
                    "audit_hash": row["audit_hash"],
                    "created_at": row["created_at"],
                }
                for row in audit_rows
            ],
            "branches": [
                {
                    "branch_uuid": row["branch_uuid"],
                    "proposal_uuid": row["proposal_uuid"],
                    "parent_branch_uuid": row["parent_branch_uuid"],
                    "branch_name": row["branch_name"],
                    "base_block_number": row["base_block_number"],
                    "base_block_hash": row["base_block_hash"],
                    "incident_tx_hash": row["incident_tx_hash"],
                    "status": row["status"],
                    "is_canonical": bool(row["is_canonical"]),
                    "write_enabled": bool(row["write_enabled"]) if "write_enabled" in row.keys() else bool(row["is_canonical"]),
                    "recovery_type": row["recovery_type"] if "recovery_type" in row.keys() else "canonical_pointer_only",
                    "replay_plan": _json_loads(row["replay_plan_json"], {}),
                    "created_at": row["created_at"],
                    "activated_at": row["activated_at"],
                }
                for row in branches
            ],
            "branch_tree": self._governance_branch_tree_locked(conn, branch_rows),
        }

    def ensure_wallet(self, conn, user_id):
        user_id = int(user_id)
        row = conn.execute("SELECT * FROM points_wallets WHERE user_id=?", (user_id,)).fetchone()
        if row:
            return row
        now = utc_now()
        with self._wallet_lock:
            row = conn.execute("SELECT * FROM points_wallets WHERE user_id=?", (user_id,)).fetchone()
            if row:
                return row
            conn.execute(
                """
                INSERT OR IGNORE INTO points_wallets (user_id, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (user_id, now, now),
            )
            return conn.execute("SELECT * FROM points_wallets WHERE user_id=?", (user_id,)).fetchone()

    def _pc0_internal_system_address(self, name):
        return "pc0" + sha256_text(f"pc0_internal:{str(name or '').strip().lower()}:{self.chain_secret or ''}")[:48]

    def _deposit_address_for_user(self, user_id, *, chain="points_chain_sim", vault_key="default", version=1):
        return "pc1" + sha256_text(
            f"deposit_address:{str(chain or 'points_chain_sim')}:{str(vault_key or 'default')}:{int(version or 1)}:{int(user_id)}:{self.chain_secret or ''}"
        )[:48]

    def _ensure_user_deposit_address_locked(self, conn, user_id, *, chain="points_chain_sim", vault_key="default", version=1):
        user_id = int(user_id)
        chain = str(chain or "points_chain_sim").strip() or "points_chain_sim"
        vault_key = str(vault_key or "default").strip() or "default"
        version = max(1, int(version or 1))
        address = self._deposit_address_for_user(user_id, chain=chain, vault_key=vault_key, version=version)
        if is_pc0_internal_address(address):
            raise ValueError("deposit address generator must not allocate pc0 namespace")
        row = conn.execute(
            """
            SELECT * FROM points_chain_deposit_addresses
            WHERE user_id=? AND chain=? AND status='active'
            ORDER BY id ASC
            LIMIT 1
            """,
            (user_id, chain),
        ).fetchone()
        now = utc_now()
        if row:
            return dict(row)
        conn.execute(
            """
            INSERT INTO points_chain_deposit_addresses (
                user_id, chain, address, vault_key, status, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                user_id,
                chain,
                address,
                vault_key,
                _json_dumps({
                    "role": "platform_controlled_deposit_address",
                    "credits_to": "pc0_official_hot_wallet_after_confirmation",
                    "deposit_address_version": version,
                }),
                now,
                now,
            ),
        )
        return dict(conn.execute("SELECT * FROM points_chain_deposit_addresses WHERE user_id=? AND chain=? AND status='active'", (user_id, chain)).fetchone())

    def ensure_user_deposit_address(self, conn, user_id, *, chain="points_chain_sim"):
        return self._ensure_user_deposit_address_locked(conn, user_id, chain=chain)

    def _deposit_bridge_pending_reason(self, *, confirmations, required_confirmations, risk_status):
        risk_status = str(risk_status or "accepted").strip().lower()
        if risk_status == "blocked":
            return "risk_blocked"
        if risk_status != "accepted":
            return "risk_review"
        if int(confirmations or 0) < int(required_confirmations or 20):
            return "confirmations_pending"
        return ""

    def _deposit_bridge_status_for_state(self, *, confirmations, required_confirmations, risk_status):
        risk_status = str(risk_status or "accepted").strip().lower()
        if risk_status == "blocked":
            return "failed"
        if confirmations < required_confirmations or risk_status != "accepted":
            return "pending"
        return "credited"

    def _deposit_bridge_public_metadata(
        self,
        *,
        bridge_uuid,
        source,
        destination,
        hot_wallet_address,
        deposit_address,
        tx_hash,
        confirmations,
        required_confirmations,
        risk_status,
        metadata=None,
    ):
        return {
            "settlement_rail": "deposit_bridge_credit",
            "chain_required": False,
            "approval_required": False,
            "network_fee_points": 0,
            "service_fee_points": 0,
            "source_wallet_address": self._pc0_internal_system_address("deposit_clearing"),
            "source_wallet_label": "pc0 入金清算錢包",
            "destination_wallet_address": hot_wallet_address,
            "deposit_address": deposit_address,
            "external_source_address": source,
            "reference_chain_tx_hash": tx_hash,
            "confirmations": int(confirmations or 0),
            "required_confirmations": int(required_confirmations or 20),
            "risk_status": str(risk_status or "accepted").strip().lower(),
            "bridge_uuid": bridge_uuid,
            **(metadata if isinstance(metadata, dict) else {}),
        }

    def _deposit_bridge_credit_locked(
        self,
        conn,
        *,
        actor,
        bridge_row,
        user_id,
        amount,
        chain,
        tx_hash,
        source,
        destination,
        hot_wallet_address,
        deposit_address,
        confirmations,
        required_confirmations,
        network_fee_points=0,
        metadata=None,
    ):
        bridge_uuid = str(bridge_row["bridge_uuid"] if bridge_row else "").strip() or str(uuid.uuid4())
        network_fee_points = max(0, int(network_fee_points or 0))
        public_metadata = self._deposit_bridge_public_metadata(
            bridge_uuid=bridge_uuid,
            source=source,
            destination=destination,
            hot_wallet_address=hot_wallet_address,
            deposit_address=deposit_address,
            tx_hash=tx_hash,
            confirmations=confirmations,
            required_confirmations=required_confirmations,
            risk_status="accepted",
            metadata={
                "external_network_fee_points": network_fee_points,
                **(metadata if isinstance(metadata, dict) else {}),
            },
        )
        ledger_uuid = str(bridge_row["internal_ledger_uuid"] if bridge_row and "internal_ledger_uuid" in bridge_row.keys() else "" or "")
        ledger_row = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (ledger_uuid,)).fetchone() if ledger_uuid else None
        created = False
        if not ledger_row:
            ledger_row, created = self._record_transaction(
                conn,
                user_id=int(user_id),
                currency_type=DISPLAY_CURRENCY,
                direction="credit",
                amount=amount,
                action_type="deposit_credit",
                reference_type="deposit_bridge",
                reference_id=tx_hash,
                idempotency_key=f"deposit_bridge_credit:{chain}:{tx_hash}",
                reason="DEPOSIT_BRIDGE_CREDIT_TO_PC0_HOT_WALLET",
                public_metadata=public_metadata,
                actor=actor,
            )
        now = utc_now()
        if bridge_row:
            conn.execute(
                """
                UPDATE points_chain_bridge_events
                SET confirmations=?,
                    required_confirmations=?,
                    network_fee_points=?,
                    risk_status='accepted',
                    status='credited',
                    internal_ledger_uuid=?,
                    metadata_json=?,
                    confirmed_at=COALESCE(confirmed_at, ?),
                    credited_at=COALESCE(credited_at, ?),
                    updated_at=?
                WHERE id=?
                """,
                (
                    int(confirmations),
                    int(required_confirmations),
                    network_fee_points,
                    ledger_row["ledger_uuid"],
                    _metadata_json_checked(public_metadata, label="deposit bridge metadata"),
                    now,
                    now,
                    now,
                    int(bridge_row["id"]),
                ),
            )
            bridge = conn.execute("SELECT * FROM points_chain_bridge_events WHERE id=?", (int(bridge_row["id"]),)).fetchone()
        else:
            conn.execute(
                """
                INSERT INTO points_chain_bridge_events (
                    bridge_uuid, bridge_type, user_id, chain, chain_tx_hash,
                    source_address, destination_address, hot_wallet_address, amount_points,
                    network_fee_points, confirmations, required_confirmations, risk_status,
                    status, internal_ledger_uuid, metadata_json, created_at, confirmed_at, credited_at, updated_at
                ) VALUES (?, 'deposit', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', 'credited', ?, ?, ?, ?, ?, ?)
                """,
                (
                    bridge_uuid,
                    int(user_id),
                    chain,
                    tx_hash,
                    source,
                    destination,
                    hot_wallet_address,
                    amount,
                    network_fee_points,
                    int(confirmations),
                    int(required_confirmations),
                    ledger_row["ledger_uuid"],
                    _metadata_json_checked(public_metadata, label="deposit bridge metadata"),
                    now,
                    now,
                    now,
                    now,
                ),
            )
            bridge = conn.execute("SELECT * FROM points_chain_bridge_events WHERE bridge_uuid=?", (bridge_uuid,)).fetchone()
        self._notify_deposit_bridge_credited(
            conn,
            user_id=int(user_id),
            amount=amount,
            tx_hash=tx_hash,
            source_address=source,
            deposit_address=deposit_address,
            hot_wallet_address=hot_wallet_address,
        )
        return bridge, ledger_row, created

    def _active_deposit_addresses_for_user(self, conn, user_id):
        try:
            rows = conn.execute(
                """
                SELECT *
                FROM points_chain_deposit_addresses
                WHERE user_id=? AND status='active'
                ORDER BY chain ASC, id ASC
                """,
                (int(user_id),),
            ).fetchall()
        except Exception:
            return []
        return [
            {
                "chain": row["chain"],
                "address": row["address"],
                "status": row["status"],
                "vault_key": row["vault_key"],
                "metadata": _json_loads(row["metadata_json"], {}),
            }
            for row in rows
        ]

    def _active_deposit_address_for_address(self, conn, address, *, include_rotated=False):
        address = str(address or "").strip().lower()
        if not address:
            return None
        statuses = ("active", "rotated") if include_rotated else ("active",)
        placeholders = ", ".join("?" for _ in statuses)
        try:
            return conn.execute(
                f"""
                SELECT *
                FROM points_chain_deposit_addresses
                WHERE address=?
                  AND status IN ({placeholders})
                ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, id DESC
                LIMIT 1
                """,
                (address, *statuses),
            ).fetchone()
        except Exception:
            return None

    def _notify_deposit_bridge_credited(self, conn, *, user_id, amount, tx_hash, source_address, deposit_address, hot_wallet_address):
        return self._notify_wallet_transfer_once(
            conn,
            user_id=int(user_id),
            notification_type="points_chain_deposit_bridge_credited",
            title="入金已轉入站內錢包",
            body=(
                f"鏈上交易 {tx_hash} 已轉入你的平台入金地址 {deposit_address}；"
                f"已 credit {int(amount)} 點到官方熱錢包 {hot_wallet_address}。來源：{source_address}"
            ),
            tx_group_hash=tx_hash,
        )

    def confirm_deposit_to_hot_wallet(
        self,
        *,
        actor,
        user_id,
        source_address,
        destination_address,
        amount_points,
        chain_tx_hash="",
        chain="points_chain_sim",
        confirmations=20,
        required_confirmations=20,
        risk_status="accepted",
        metadata=None,
    ):
        amount = int(amount_points or 0)
        if amount <= 0:
            raise ValueError("amount_points must be positive")
        source = str(source_address or "").strip().lower()
        if not source or is_pc0_internal_address(source):
            raise ValueError("deposit source must be an external or cold-chain address, not pc0")
        destination = str(destination_address or "").strip().lower()
        if not destination:
            raise ValueError("deposit destination address required")
        if is_pc0_internal_address(destination):
            raise ValueError("deposit destination must be a platform chain deposit address, not pc0")
        chain = str(chain or "points_chain_sim").strip() or "points_chain_sim"
        confirmations = max(0, int(confirmations or 0))
        required_confirmations = max(1, int(required_confirmations or 20))
        risk_status = str(risk_status or "accepted").strip().lower()
        if risk_status not in {"accepted", "review", "blocked"}:
            raise ValueError("deposit risk_status is invalid")
        tx_hash = str(chain_tx_hash or "").strip().lower()
        if not tx_hash:
            tx_hash = sha256_text(f"deposit:{chain}:{source}:{int(user_id)}:{amount}:{uuid.uuid4()}")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            self._assert_chain_writable(conn, "deposit bridge credit")
            active_branch = self._canonical_branch_uuid(conn)
            self._assert_canonical_write_branch(conn, active_branch)
            hot_wallet = create_official_hot_wallet(conn, user_id=int(user_id), chain_secret=self.chain_secret)
            deposit_address = self.ensure_user_deposit_address(conn, user_id, chain=chain)
            if destination != str(deposit_address["address"] or "").strip().lower():
                raise ValueError("deposit destination does not match user's active deposit address")
            existing = conn.execute(
                """
                SELECT * FROM points_chain_bridge_events
                WHERE chain=? AND chain_tx_hash=?
                  AND chain_tx_hash<>''
                LIMIT 1
                """,
                (chain, tx_hash),
            ).fetchone()
            if existing:
                if int(existing["user_id"] or 0) != int(user_id):
                    raise ValueError("deposit chain_tx_hash is already assigned to another user")
                if str(existing["bridge_type"] or "deposit") != "deposit":
                    raise ValueError("deposit chain_tx_hash belongs to a non-deposit bridge event")
                if int(existing["amount_points"] or 0) != amount:
                    raise ValueError("deposit chain_tx_hash idempotency conflict")
                if str(existing["source_address"] or "").strip().lower() != source:
                    raise ValueError("deposit chain_tx_hash source mismatch")
                if str(existing["destination_address"] or "").strip().lower() != destination:
                    raise ValueError("deposit chain_tx_hash destination mismatch")
                previous_status = str(existing["status"] or "pending").strip().lower()
                previous_risk = str(existing["risk_status"] or "accepted").strip().lower()
                effective_confirmations = max(int(existing["confirmations"] or 0), confirmations)
                effective_required = max(int(existing["required_confirmations"] or 20), required_confirmations)
                effective_risk = "blocked" if previous_risk == "blocked" or risk_status == "blocked" else risk_status
                existing_ledger = conn.execute(
                    "SELECT * FROM points_ledger WHERE ledger_uuid=?",
                    (existing["internal_ledger_uuid"] or "",),
                ).fetchone() if existing["internal_ledger_uuid"] else None
                if previous_status == "credited" or existing_ledger:
                    if previous_status != "credited" and existing_ledger:
                        now = utc_now()
                        conn.execute(
                            """
                            UPDATE points_chain_bridge_events
                            SET status='credited',
                                internal_ledger_uuid=?,
                                credited_at=COALESCE(credited_at, ?),
                                updated_at=?
                            WHERE id=?
                            """,
                            (existing_ledger["ledger_uuid"], now, now, int(existing["id"])),
                        )
                        existing = conn.execute("SELECT * FROM points_chain_bridge_events WHERE id=?", (int(existing["id"]),)).fetchone()
                    conn.commit()
                    return {
                        "ok": True,
                        "created": False,
                        "credited": True,
                        "ledger_created": False,
                        "bridge_event": dict(existing),
                        "ledger": self.serialize_ledger(existing_ledger, include_user_id=True) if existing_ledger else None,
                        "wallet": self.wallet_payload_for_read(conn, int(user_id)),
                        "deposit_address": deposit_address,
                    }
                if previous_status in {"failed", "refunded"} or effective_risk == "blocked":
                    now = utc_now()
                    public_metadata = self._deposit_bridge_public_metadata(
                        bridge_uuid=existing["bridge_uuid"],
                        source=source,
                        destination=destination,
                        hot_wallet_address=hot_wallet["address"],
                        deposit_address=deposit_address["address"],
                        tx_hash=tx_hash,
                        confirmations=effective_confirmations,
                        required_confirmations=effective_required,
                        risk_status=effective_risk,
                        metadata=metadata,
                    )
                    conn.execute(
                        """
                        UPDATE points_chain_bridge_events
                        SET confirmations=?,
                            required_confirmations=?,
                            risk_status=?,
                            status='failed',
                            metadata_json=?,
                            confirmed_at=CASE WHEN ? >= ? THEN COALESCE(confirmed_at, ?) ELSE confirmed_at END,
                            updated_at=?
                        WHERE id=?
                        """,
                        (
                            effective_confirmations,
                            effective_required,
                            effective_risk,
                            _metadata_json_checked(public_metadata, label="deposit bridge metadata"),
                            effective_confirmations,
                            effective_required,
                            now,
                            now,
                            int(existing["id"]),
                        ),
                    )
                    bridge = conn.execute("SELECT * FROM points_chain_bridge_events WHERE id=?", (int(existing["id"]),)).fetchone()
                    conn.commit()
                    return {
                        "ok": True,
                        "created": False,
                        "credited": False,
                        "bridge_event": dict(bridge),
                        "ledger": None,
                        "wallet": self.wallet_payload_for_read(conn, int(user_id)),
                        "deposit_address": deposit_address,
                        "reason": "risk_blocked",
                    }
                if effective_confirmations >= effective_required and effective_risk == "accepted":
                    bridge, ledger, ledger_created = self._deposit_bridge_credit_locked(
                        conn,
                        actor=actor,
                        bridge_row=existing,
                        user_id=int(user_id),
                        amount=amount,
                        chain=chain,
                        tx_hash=tx_hash,
                        source=source,
                        destination=destination,
                        hot_wallet_address=hot_wallet["address"],
                        deposit_address=deposit_address["address"],
                        confirmations=effective_confirmations,
                        required_confirmations=effective_required,
                        metadata=metadata,
                    )
                    conn.commit()
                    return {
                        "ok": True,
                        "created": False,
                        "credited": True,
                        "ledger_created": bool(ledger_created),
                        "bridge_event": dict(bridge),
                        "ledger": self.serialize_ledger(ledger, include_user_id=True),
                        "wallet": self.wallet_payload_for_read(conn, int(user_id)),
                        "deposit_address": deposit_address,
                    }
                now = utc_now()
                public_metadata = self._deposit_bridge_public_metadata(
                    bridge_uuid=existing["bridge_uuid"],
                    source=source,
                    destination=destination,
                    hot_wallet_address=hot_wallet["address"],
                    deposit_address=deposit_address["address"],
                    tx_hash=tx_hash,
                    confirmations=effective_confirmations,
                    required_confirmations=effective_required,
                    risk_status=effective_risk,
                    metadata=metadata,
                )
                conn.execute(
                    """
                    UPDATE points_chain_bridge_events
                    SET confirmations=?,
                        required_confirmations=?,
                        risk_status=?,
                        status='pending',
                        metadata_json=?,
                        confirmed_at=CASE WHEN ? >= ? THEN COALESCE(confirmed_at, ?) ELSE confirmed_at END,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        effective_confirmations,
                        effective_required,
                        effective_risk,
                        _metadata_json_checked(public_metadata, label="deposit bridge metadata"),
                        effective_confirmations,
                        effective_required,
                        now,
                        now,
                        int(existing["id"]),
                    ),
                )
                bridge = conn.execute("SELECT * FROM points_chain_bridge_events WHERE id=?", (int(existing["id"]),)).fetchone()
                conn.commit()
                return {
                    "ok": True,
                    "created": False,
                    "credited": False,
                    "bridge_event": dict(bridge),
                    "ledger": None,
                    "wallet": self.wallet_payload_for_read(conn, int(user_id)),
                    "deposit_address": deposit_address,
                    "reason": self._deposit_bridge_pending_reason(
                        confirmations=effective_confirmations,
                        required_confirmations=effective_required,
                        risk_status=effective_risk,
                    ),
                }
            bridge_uuid = str(uuid.uuid4())
            public_metadata = {
                "settlement_rail": "deposit_bridge_credit",
                "chain_required": False,
                "approval_required": False,
                "network_fee_points": 0,
                "service_fee_points": 0,
                "source_wallet_address": self._pc0_internal_system_address("deposit_clearing"),
                "source_wallet_label": "pc0 入金清算錢包",
                "destination_wallet_address": hot_wallet["address"],
                "deposit_address": deposit_address["address"],
                "external_source_address": source,
                "reference_chain_tx_hash": tx_hash,
                "confirmations": confirmations,
                "required_confirmations": required_confirmations,
                "risk_status": risk_status,
                "bridge_uuid": bridge_uuid,
                **(metadata if isinstance(metadata, dict) else {}),
            }
            now = utc_now()
            if confirmations < required_confirmations or risk_status != "accepted":
                status = "failed" if risk_status == "blocked" else "pending"
                pending_reason = (
                    "risk_blocked"
                    if risk_status == "blocked"
                    else "risk_review"
                    if risk_status != "accepted"
                    else "confirmations_pending"
                )
                conn.execute(
                    """
                    INSERT INTO points_chain_bridge_events (
                        bridge_uuid, bridge_type, user_id, chain, chain_tx_hash,
                        source_address, destination_address, hot_wallet_address, amount_points,
                        network_fee_points, confirmations, required_confirmations, risk_status,
                        status, internal_ledger_uuid, metadata_json, created_at, confirmed_at, updated_at
                    ) VALUES (?, 'deposit', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, '', ?, ?, ?, ?)
                    """,
                    (
                        bridge_uuid,
                        int(user_id),
                        chain,
                        tx_hash,
                        source,
                        destination,
                        hot_wallet["address"],
                        amount,
                        confirmations,
                        required_confirmations,
                        risk_status,
                        status,
                        _metadata_json_checked(public_metadata, label="deposit bridge metadata"),
                        now,
                        now if confirmations >= required_confirmations else None,
                        now,
                    ),
                )
                bridge = conn.execute("SELECT * FROM points_chain_bridge_events WHERE bridge_uuid=?", (bridge_uuid,)).fetchone()
                conn.commit()
                return {
                    "ok": True,
                    "created": True,
                    "credited": False,
                    "bridge_event": dict(bridge),
                    "ledger": None,
                    "wallet": self.wallet_payload_for_read(conn, int(user_id)),
                    "deposit_address": deposit_address,
                    "reason": pending_reason,
                }
            ledger_row, created = self._record_transaction(
                conn,
                user_id=int(user_id),
                currency_type=DISPLAY_CURRENCY,
                direction="credit",
                amount=amount,
                action_type="deposit_credit",
                reference_type="deposit_bridge",
                reference_id=tx_hash,
                idempotency_key=f"deposit_bridge_credit:{chain}:{tx_hash}",
                reason="DEPOSIT_BRIDGE_CREDIT_TO_PC0_HOT_WALLET",
                public_metadata=public_metadata,
                actor=actor,
            )
            conn.execute(
                """
                INSERT INTO points_chain_bridge_events (
                    bridge_uuid, bridge_type, user_id, chain, chain_tx_hash,
                    source_address, destination_address, hot_wallet_address, amount_points,
                    network_fee_points, confirmations, required_confirmations, risk_status,
                    status, internal_ledger_uuid, metadata_json, created_at, confirmed_at, credited_at, updated_at
                ) VALUES (?, 'deposit', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'accepted', 'credited', ?, ?, ?, ?, ?, ?)
                """,
                (
                    bridge_uuid,
                    int(user_id),
                    chain,
                    tx_hash,
                    source,
                    destination,
                    hot_wallet["address"],
                    amount,
                    confirmations,
                    required_confirmations,
                    ledger_row["ledger_uuid"],
                    _metadata_json_checked(public_metadata, label="deposit bridge metadata"),
                    now,
                    now,
                    now,
                    now,
                ),
            )
            bridge = conn.execute("SELECT * FROM points_chain_bridge_events WHERE bridge_uuid=?", (bridge_uuid,)).fetchone()
            conn.commit()
            return {
                "ok": True,
                "created": bool(created),
                "credited": True,
                "bridge_event": dict(bridge),
                "ledger": self.serialize_ledger(ledger_row, include_user_id=True),
                "wallet": self.wallet_payload_for_read(conn, int(user_id)),
                "deposit_address": deposit_address,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def wallet_payload_for_read(self, conn, user_id):
        user_id = int(user_id)
        row = conn.execute("SELECT * FROM points_wallets WHERE user_id=?", (user_id,)).fetchone()
        if row:
            payload = self.serialize_wallet(row)
        else:
            payload = {
                "user_id": user_id,
                "public_account_id": self._public_account_id(user_id),
                "currency_type": DISPLAY_CURRENCY,
                "points_balance": 0,
                "points_frozen": 0,
                "total_points_earned": 0,
                "total_points_spent": 0,
                "soft_balance": 0,
                "hard_balance": 0,
                "soft_frozen": 0,
                "hard_frozen": 0,
                "wallet_status": "active",
                "risk_level": "normal",
                "created_at": None,
                "updated_at": None,
            }
        statement = self._wallet_statement_totals_for_user(conn, user_id)
        active_branch = self._canonical_branch_uuid(conn)
        payload["chain_branch"] = active_branch
        payload["branch"] = self._branch_metadata(conn, active_branch)
        payload["total_points_earned"] = int(statement["earned"])
        payload["total_points_spent"] = int(statement["spent"])
        payload["total_soft_earned"] = int(statement["earned"])
        payload["total_soft_spent"] = int(statement["spent"])
        payload["total_hard_earned"] = 0
        payload["total_hard_spent"] = 0
        deposit_addresses = self._active_deposit_addresses_for_user(conn, user_id)
        payload["deposit_addresses"] = deposit_addresses
        payload["deposit_address"] = deposit_addresses[0]["address"] if deposit_addresses else ""
        payload["deposit_model"] = "external_or_cold_chain_to_platform_deposit_address_then_internal_pc0_credit"
        identity_balances = self._wallet_identity_balances_for_user(conn, user_id, account_statement=statement)
        payload["account_points_balance"] = int(payload.get("points_balance") or 0)
        payload["account_points_frozen"] = int(payload.get("points_frozen") or 0)
        payload["active_wallet_address"] = identity_balances.get("primary_address") or ""
        payload["wallet_identity_source"] = "legacy_account"
        if identity_balances.get("has_identity"):
            active_address = identity_balances.get("primary_address") or ""
            active = (identity_balances.get("balances") or {}).get(active_address, {"balance": 0, "frozen": 0})
            active_balance = int(active.get("balance") or 0)
            active_frozen = int(active.get("frozen") or 0)
            account_balance = sum(int(item.get("balance") or 0) for item in (identity_balances.get("balances") or {}).values())
            account_frozen = sum(int(item.get("frozen") or 0) for item in (identity_balances.get("balances") or {}).values())
            payload["account_points_balance"] = account_balance
            payload["account_points_frozen"] = account_frozen
            payload["points_balance"] = active_balance
            payload["points_frozen"] = active_frozen
            payload["soft_balance"] = active_balance
            payload["soft_frozen"] = active_frozen
            payload["hard_balance"] = 0
            payload["hard_frozen"] = 0
            payload["wallet_identity_source"] = "active_wallet" if active_address else "no_active_wallet"
            payload["wallet_identity_balances"] = {
                address: {
                    "points_balance": int(item.get("balance") or 0),
                    "points_frozen": int(item.get("frozen") or 0),
                    "pending_outgoing_points": int(item.get("pending_outgoing") or 0),
                }
                for address, item in sorted((identity_balances.get("balances") or {}).items())
            }
        return payload

    def get_wallet(self, user_id):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            wallet = self.ensure_wallet(conn, user_id)
            create_official_hot_wallet(conn, user_id=int(user_id), chain_secret=self.chain_secret)
            self.ensure_user_deposit_address(conn, user_id)
            self._reconcile_confirmed_deposit_transfers_locked(
                conn,
                actor={"role": "system", "id": None},
                limit=1000,
            )
            conn.commit()
            return self.wallet_payload_for_read(conn, user_id)
        finally:
            conn.close()

    def get_wallet_snapshot(self, user_id):
        user_id = int(user_id)
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self.ensure_wallet(conn, user_id)
            create_official_hot_wallet(conn, user_id=user_id, chain_secret=self.chain_secret)
            self.ensure_user_deposit_address(conn, user_id)
            conn.commit()
            row = conn.execute("SELECT * FROM points_wallets WHERE user_id=?", (user_id,)).fetchone()
            if row:
                payload = self.serialize_wallet(row)
            else:
                payload = {
                    "user_id": user_id,
                    "public_account_id": self._public_account_id(user_id),
                    "currency_type": DISPLAY_CURRENCY,
                    "points_balance": 0,
                    "points_frozen": 0,
                    "total_points_earned": 0,
                    "total_points_spent": 0,
                    "soft_balance": 0,
                    "hard_balance": 0,
                    "soft_frozen": 0,
                    "hard_frozen": 0,
                    "total_soft_earned": 0,
                    "total_hard_earned": 0,
                    "total_soft_spent": 0,
                    "total_hard_spent": 0,
                    "wallet_status": "active",
                    "risk_level": "normal",
                    "created_at": None,
                    "updated_at": None,
                }
            active_branch = self._canonical_branch_uuid(conn)
            payload["chain_branch"] = active_branch
            payload["branch"] = self._branch_metadata(conn, active_branch)
            deposit_addresses = self._active_deposit_addresses_for_user(conn, user_id)
            payload["deposit_addresses"] = deposit_addresses
            payload["deposit_address"] = deposit_addresses[0]["address"] if deposit_addresses else ""
            payload["deposit_model"] = "external_or_cold_chain_to_platform_deposit_address_then_internal_pc0_credit"
            payload["wallet_identity_source"] = "wallet_snapshot"
            try:
                state = self._wallet_identity_state_for_user(conn, user_id)
            except Exception:
                state = {"has_identity": False, "primary": None, "addresses": set()}
            if state.get("has_identity"):
                materialized_rows = conn.execute(
                    """
                    SELECT *
                    FROM points_wallet_identity_balances
                    WHERE chain_branch=? AND user_id=?
                    ORDER BY wallet_address ASC
                    """,
                    (active_branch, user_id),
                ).fetchall()
                balances = {
                    str(row["wallet_address"] or "").strip().lower(): {
                        "points_balance": int(row["available_points"] or 0),
                        "points_frozen": int(row["frozen_points"] or 0),
                        "pending_outgoing_points": int(row["pending_outgoing_points"] or 0),
                    }
                    for row in materialized_rows
                }
                primary = state.get("primary")
                if isinstance(primary, dict):
                    active_address = str(primary.get("address") or "").strip().lower()
                elif primary is not None:
                    try:
                        active_address = str(primary["address"] or "").strip().lower()
                    except Exception:
                        active_address = ""
                else:
                    active_address = ""
                active = balances.get(active_address, {"points_balance": 0, "points_frozen": 0, "pending_outgoing_points": 0})
                account_balance = sum(int(item.get("points_balance") or 0) for item in balances.values())
                account_frozen = sum(int(item.get("points_frozen") or 0) for item in balances.values())
                if not balances:
                    account_balance = int(payload.get("points_balance") or 0)
                    account_frozen = int(payload.get("points_frozen") or 0)
                    balances = {
                        active_address: {
                            "points_balance": account_balance,
                            "points_frozen": account_frozen,
                            "pending_outgoing_points": 0,
                        }
                    } if active_address else {}
                    active = balances.get(active_address, {
                        "points_balance": account_balance,
                        "points_frozen": account_frozen,
                        "pending_outgoing_points": 0,
                    })
                payload.update({
                    "account_points_balance": account_balance,
                    "account_points_frozen": account_frozen,
                    "active_wallet_address": active_address,
                    "points_balance": int(active.get("points_balance") or 0),
                    "points_frozen": int(active.get("points_frozen") or 0),
                    "soft_balance": int(active.get("points_balance") or 0),
                    "soft_frozen": int(active.get("points_frozen") or 0),
                    "hard_balance": 0,
                    "hard_frozen": 0,
                    "wallet_identity_balances": balances,
                    "wallet_identity_source": "materialized_wallet_identity_balances_snapshot",
                })
            else:
                payload["account_points_balance"] = int(payload.get("points_balance") or 0)
                payload["account_points_frozen"] = int(payload.get("points_frozen") or 0)
                payload["active_wallet_address"] = ""
                payload["wallet_identity_balances"] = {}
            payload["snapshot"] = {
                "snapshot_backed": True,
                "bounded": True,
                "source": payload.get("wallet_identity_source") or "wallet_snapshot",
            }
            return payload
        finally:
            conn.close()

    def wallet_creation_fee_quote_for_count(self, wallet_count):
        count = max(0, int(wallet_count or 0))
        if count <= 0:
            amount = 0
        else:
            amount = WALLET_CREATION_FEE_BASE_POINTS * (WALLET_CREATION_FEE_MULTIPLIER ** (count - 1))
            amount = min(amount, WALLET_CREATION_FEE_MAX_POINTS)
        next_wallet_number = count + 1
        return {
            "item_key": "wallet_creation_fee",
            "amount_points": int(amount),
            "currency_type": DISPLAY_CURRENCY,
            "existing_wallet_count": count,
            "next_wallet_number": next_wallet_number,
            "base_points": WALLET_CREATION_FEE_BASE_POINTS,
            "multiplier": WALLET_CREATION_FEE_MULTIPLIER,
            "max_points": WALLET_CREATION_FEE_MAX_POINTS,
            "destination_fund_key": "official_treasury",
            "reference_type": "wallet_identity",
            "reference_id": f"wallet_identity:create:{next_wallet_number}",
            "chargeable_wallet_types": ["self_custody_cold", "imported_cold"],
            "existing_chargeable_wallet_count": count,
            "formula": "first cold wallet free; nth paid cold wallet = min(base * multiplier^(existing_chargeable_wallet_count - 1), max)",
        }

    def wallet_creation_chargeable_wallet_count(self, conn, user_id):
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM (
                SELECT LOWER(address) AS address
                FROM points_wallet_identities
                WHERE user_id=?
                  AND status IN ('pending_backup', 'active')
                  AND wallet_type IN ('self_custody_cold', 'imported_cold')
                  AND custody_mode='self_custody'
                UNION
                SELECT LOWER(address) AS address
                FROM points_wallet_identity_bindings
                WHERE user_id=?
                  AND status='active'
                  AND wallet_type IN ('self_custody_cold', 'imported_cold')
            )
            """,
            (int(user_id), int(user_id)),
        ).fetchone()
        return int((row["count"] if row else 0) or 0)

    def wallet_creation_fee_quote(self, conn, user_id):
        return self.wallet_creation_fee_quote_for_count(
            self.wallet_creation_chargeable_wallet_count(conn, user_id)
        )

    def charge_wallet_creation_fee_locked(
        self,
        conn,
        *,
        user_id,
        source_wallet_address,
        request_uuid,
        signature="",
        wallet_count_before,
        amount_points=None,
        mode="wallet",
        actor=None,
    ):
        quote = self.wallet_creation_fee_quote_for_count(wallet_count_before)
        expected_amount = int(quote["amount_points"])
        if expected_amount <= 0:
            return {"charged": False, "amount_points": 0, "quote": quote, "ledger": None}
        if amount_points is not None and int(amount_points) != expected_amount:
            raise ValueError("wallet creation fee quote changed; refresh wallet management before retrying")
        source_address = str(source_wallet_address or "").strip().lower()
        if not source_address:
            raise ValueError("creating a second or later wallet requires a fee source wallet")
        if not request_uuid:
            raise ValueError("wallet creation fee request_uuid is required")
        source_wallet = self._wallet_identity_row_for_user_address(conn, user_id, source_address, active_only=True)
        if not source_wallet:
            raise ValueError("fee source wallet does not belong to current user or is inactive")
        self._assert_wallet_identity_can_spend(source_wallet, context="wallet creation fee")
        custody_mode = str(source_wallet["custody_mode"] or "")
        if custody_mode == "multisig":
            raise PermissionError("multisig fee source requires a multisig payment flow; choose a hot or self-custody wallet")
        if custody_mode == "self_custody":
            self._verify_service_fee_wallet_signature(
                conn=conn,
                user_id=user_id,
                source_wallet_address=source_address,
                item_key=quote["item_key"],
                quantity=1,
                amount_points=expected_amount,
                request_uuid=request_uuid,
                reference_type=quote["reference_type"],
                reference_id=quote["reference_id"],
                signature=signature,
            )
        available = self._wallet_identity_available_for_address(conn, user_id=int(user_id), address=source_address)
        if available < expected_amount:
            raise ValueError("insufficient balance for wallet creation fee")
        row, created = self._record_transaction(
            conn,
            user_id=user_id,
            currency_type=DISPLAY_CURRENCY,
            direction="debit",
            amount=expected_amount,
            action_type="wallet_creation_fee",
            reference_type=quote["reference_type"],
            reference_id=quote["reference_id"],
            idempotency_key=f"wallet_creation_fee:{request_uuid}",
            reason=f"wallet creation fee for {mode}",
            public_metadata={
                "source_wallet_address": source_address,
                "wallet_creation_fee": True,
                "creation_mode": str(mode or "wallet"),
                "existing_wallet_count": int(wallet_count_before or 0),
                "next_wallet_number": int(quote["next_wallet_number"]),
                "destination_fund_key": "official_treasury",
            },
            actor=actor,
        )
        return {
            "charged": bool(created),
            "amount_points": expected_amount,
            "quote": quote,
            "ledger": self.serialize_ledger(row),
        }

    def serialize_wallet(self, row):
        if not row:
            return None
        points_balance = int(row["soft_balance"] or 0) + int(row["hard_balance"] or 0)
        points_frozen = int(row["soft_frozen"] or 0) + int(row["hard_frozen"] or 0)
        total_points_earned = int(row["total_soft_earned"] or 0) + int(row["total_hard_earned"] or 0)
        total_points_spent = int(row["total_soft_spent"] or 0) + int(row["total_hard_spent"] or 0)
        return {
            "user_id": row["user_id"],
            "public_account_id": self._public_account_id(row["user_id"]),
            "currency_type": DISPLAY_CURRENCY,
            "points_balance": points_balance,
            "points_frozen": points_frozen,
            "total_points_earned": total_points_earned,
            "total_points_spent": total_points_spent,
            "soft_balance": points_balance,
            "hard_balance": 0,
            "soft_frozen": points_frozen,
            "hard_frozen": 0,
            "total_soft_earned": total_points_earned,
            "total_hard_earned": 0,
            "total_soft_spent": total_points_spent,
            "total_hard_spent": 0,
            "wallet_status": row["wallet_status"],
            "risk_level": row["risk_level"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def serialize_ledger(self, row, *, include_user_id=False):
        data = {
            "id": row["id"],
            "ledger_uuid": row["ledger_uuid"],
            "chain_branch": row["chain_branch"] if "chain_branch" in row.keys() else "main",
            "public_account_id": row["public_account_id"],
            "currency_type": display_currency_type(row["currency_type"]),
            "direction": row["direction"],
            "amount": row["amount"],
            "balance_before": row["balance_before"],
            "balance_after": row["balance_after"],
            "action_type": row["action_type"],
            "reference_type": row["reference_type"],
            "reference_id": row["reference_id"],
            "reason": row["reason"],
            "public_metadata": _json_loads(row["public_metadata_json"], {}),
            "metadata_hash": row["metadata_hash"],
            "previous_ledger_hash": row["previous_ledger_hash"],
            "ledger_hash": row["ledger_hash"],
            "chain_block_id": row["chain_block_id"],
            "risk_flag": row["risk_flag"],
            "risk_score": row["risk_score"],
            "status": row["status"],
            "created_at": row["created_at"],
        }
        if include_user_id:
            data["user_id"] = row["user_id"]
            data["created_by"] = row["created_by"]
            data["created_by_role"] = row["created_by_role"]
        return data

    def _primary_wallet_address_for_read(self, conn, user_id):
        try:
            row = get_primary_wallet_identity(conn, user_id)
            return row["address"] if row else ""
        except Exception:
            return ""

    def _active_wallet_identity_for_user(self, conn, user_id):
        try:
            row = get_primary_wallet_identity(conn, user_id)
            if row and str(row["status"] if not isinstance(row, dict) else row.get("status") or "") == "active":
                return row
            return None
        except Exception:
            return None

    def _initial_grant_entitlement_for_user(self, conn, user_id):
        cols = table_columns(conn, "users")
        if "id" not in cols or "username" not in cols or "role" not in cols:
            return None
        status_expr = "COALESCE(status, 'active')" if "status" in cols else "'active'"
        row = conn.execute(
            f"""
            SELECT id, username, role, {status_expr} AS status
            FROM users
            WHERE id=?
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        username = str(row["username"] or "")
        if not row or str(row["status"] or "active") != "active" or username == "root":
            return None
        role = str(row["role"] or "user")
        if role in {"manager", "super_admin"}:
            return {
                "grant": "admin_initial",
                "action_type": "admin_initial_grant",
                "amount": ADMIN_INITIAL_POINTS,
                "reference_type": "genesis_admin_allocation",
                "reason": "admin genesis allocation",
                "public_metadata": {"grant": "admin_initial", "amount": ADMIN_INITIAL_POINTS},
            }
        if username != "test":
            return None
        return {
            "grant": "user_initial",
            "action_type": "user_initial_grant",
            "amount": USER_INITIAL_POINTS,
            "reference_type": "genesis_user_allocation",
            "reason": "user genesis allocation",
            "public_metadata": {"grant": "user_initial", "amount": USER_INITIAL_POINTS},
        }

    def _official_hot_wallet_credit_metadata(self, user_id, metadata=None):
        payload = dict(metadata or {})
        payload.setdefault("destination_wallet_address", official_hot_wallet_address(self.chain_secret, int(user_id)))
        payload.setdefault("settlement_rail", "internal_hot_wallet")
        payload.setdefault("chain_required", False)
        payload.setdefault("approval_required", False)
        payload.setdefault("network_fee_points", 0)
        payload.setdefault("service_fee_points", 0)
        return payload

    def wallet_initial_grant_status(self, conn, user_id):
        ensure_wallet_identity_schema(conn)
        entitlement = self._initial_grant_entitlement_for_user(conn, user_id)
        if not entitlement:
            return {
                "required": False,
                "granted": False,
                "deferred_until_wallet": False,
                "grant": "",
                "action_type": "",
                "amount": 0,
                "active_wallet_address": "",
                "ledger_uuid": "",
                "ledger_hash": "",
            }
        hot_wallet = conn.execute(
            """
            SELECT address
            FROM points_wallet_identities
            WHERE user_id=? AND wallet_type='official_hot' AND status IN ('pending_backup', 'active')
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(user_id),),
        ).fetchone()
        hot_wallet_address = (hot_wallet["address"] if hot_wallet else official_hot_wallet_address(self.chain_secret, int(user_id)))
        existing = conn.execute(
            """
            SELECT ledger_uuid, ledger_hash, created_at
            FROM points_ledger
            WHERE user_id=? AND action_type=?
            ORDER BY id ASC
            LIMIT 1
            """,
            (int(user_id), entitlement["action_type"]),
        ).fetchone()
        granted = bool(existing)
        return {
            "required": not granted,
            "granted": granted,
            "deferred_until_wallet": False,
            "grant": entitlement["grant"],
            "action_type": entitlement["action_type"],
            "amount": int(entitlement["amount"]),
            "active_wallet_address": hot_wallet_address,
            "ledger_uuid": existing["ledger_uuid"] if existing else "",
            "ledger_hash": existing["ledger_hash"] if existing else "",
            "created_at": existing["created_at"] if existing else "",
        }

    def _ensure_initial_grant_official_hot_wallet_locked(self, conn, user_id):
        wallet = create_official_hot_wallet(conn, user_id=int(user_id), chain_secret=self.chain_secret)
        try:
            self.ensure_user_deposit_address(conn, user_id)
        except Exception:
            pass
        return wallet

    def _deferred_initial_grant_result(self, conn, user_id, status):
        return {
            "ok": True,
            "created": False,
            "deferred": True,
            "reason": "wallet_required",
            "initial_grant": status,
            "ledger": None,
            "wallet": self.wallet_payload_for_read(conn, user_id),
        }

    def _initial_grant_ready_or_deferred(self, *, user_id, expected_action_type, require_wallet=True):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            status = self.wallet_initial_grant_status(conn, user_id)
            if status.get("action_type") != expected_action_type:
                return {
                    "ok": True,
                    "created": False,
                    "deferred": False,
                    "not_applicable": True,
                    "initial_grant": status,
                    "ledger": None,
                    "wallet": self.wallet_payload_for_read(conn, user_id),
                }
            if require_wallet and status.get("deferred_until_wallet"):
                self._ensure_initial_grant_official_hot_wallet_locked(conn, user_id)
                conn.commit()
                status = self.wallet_initial_grant_status(conn, user_id)
                if status.get("deferred_until_wallet"):
                    return self._deferred_initial_grant_result(conn, user_id, status)
            elif require_wallet:
                self._ensure_initial_grant_official_hot_wallet_locked(conn, user_id)
                conn.commit()
            return None
        finally:
            conn.close()

    def _wallet_identity_row_for_user_address(self, conn, user_id, address, *, active_only=True):
        address = str(address or "").strip().lower()
        if not address:
            return None
        status_filter = "AND status IN ('pending_backup', 'active')" if active_only else ""
        try:
            row = conn.execute(
                f"""
                SELECT * FROM points_wallet_identities
                WHERE user_id=? AND address=?
                {status_filter}
                LIMIT 1
                """,
                (int(user_id), address),
            ).fetchone()
            if row:
                return row
            binding_status_filter = "AND b.status='active'" if active_only else ""
            binding = conn.execute(
                f"""
                SELECT w.*, b.id AS binding_id, b.user_id AS binding_user_id,
                       b.wallet_type AS binding_wallet_type, b.is_primary AS binding_is_primary,
                       b.status AS binding_status, b.label AS binding_label,
                       b.metadata_json AS binding_metadata_json,
                       b.created_at AS binding_created_at, b.updated_at AS binding_updated_at
                FROM points_wallet_identity_bindings b
                JOIN points_wallet_identities w ON w.id=b.wallet_identity_id
                WHERE b.user_id=? AND b.address=?
                  {binding_status_filter}
                LIMIT 1
                """,
                (int(user_id), address),
            ).fetchone()
            if not binding:
                return None
            data = {key: binding[key] for key in binding.keys() if not str(key).startswith("binding_")}
            metadata = _json_loads(data.get("metadata_json"), {})
            metadata.update(_json_loads(binding["binding_metadata_json"], {}))
            metadata.update({
                "address_control_model": "self_custody_private_key_controls_address_v1",
                "binding_record": True,
                "binding_id": int(binding["binding_id"]),
            })
            data.update({
                "user_id": int(binding["binding_user_id"]),
                "wallet_type": binding["binding_wallet_type"] or data.get("wallet_type"),
                "is_primary": int(binding["binding_is_primary"] or 0),
                "status": binding["binding_status"],
                "label": binding["binding_label"] or data.get("label") or "",
                "metadata_json": _json_dumps(metadata),
                "created_at": binding["binding_created_at"],
                "updated_at": binding["binding_updated_at"],
            })
            return data
        except Exception:
            return None

    def _wallet_address_for_user_flow(self, conn, user_id, preferred_address=None):
        preferred = self._wallet_identity_row_for_user_address(conn, user_id, preferred_address, active_only=True)
        if preferred_address and not preferred:
            raise ValueError("wallet address does not belong to user or is inactive")
        address = preferred["address"] if preferred else self._primary_wallet_address_for_read(conn, user_id)
        legacy_account = self._public_account_id(user_id)
        return address or legacy_account, ("用戶模擬鏈錢包" if address else "Legacy 帳本身份"), address, legacy_account

    def _wallet_identity_state_for_user(self, conn, user_id):
        try:
            rows = list_wallet_identities(conn, int(user_id), include_inactive=True)
        except Exception:
            return {"has_identity": False, "primary": None, "addresses": set(), "identity_row_count": 0, "active_identity_count": 0}
        has_identity_history = bool(rows)
        if not has_identity_history:
            try:
                has_identity_history = bool(conn.execute(
                    """
                    SELECT 1 FROM points_wallet_onboarding_events
                    WHERE user_id=?
                    LIMIT 1
                    """,
                    (int(user_id),),
                ).fetchone())
            except Exception:
                has_identity_history = False
        primary = None
        addresses = set()
        active_count = 0
        for row in rows:
            status = str(row.get("status") if isinstance(row, dict) else row["status"] or "")
            address = str(row.get("address") if isinstance(row, dict) else row["address"] or "")
            if address and status in {"pending_backup", "active"}:
                addresses.add(address)
                active_count += 1
            is_primary = bool(row.get("is_primary")) if isinstance(row, dict) else bool(int(row["is_primary"] or 0))
            if is_primary and status in {"pending_backup", "active"}:
                primary = row
        return {
            "has_identity": has_identity_history,
            "primary": primary,
            "addresses": addresses,
            "identity_row_count": len(rows),
            "active_identity_count": active_count,
        }

    def _legacy_counterparty_account(self, value):
        try:
            return self._public_account_id(int(value))
        except Exception:
            return ""

    def _legacy_ledger_wallet_flow_descriptor(self, conn, row, public_metadata=None):
        public_metadata = public_metadata if isinstance(public_metadata, dict) else {}
        direction = str(row["direction"] or "")
        action_type = str(row["action_type"] or "")
        user_address = row["public_account_id"] or self._public_account_id(row["user_id"])
        user_label = "Legacy 帳本身份"
        legacy_account = user_address
        if direction in {"credit", "transfer_in"}:
            source_address = ""
            source_label = ""
            source_fund_key = self._points_ledger_credit_source_fund(action_type)
            destination_fund_key = None
            destination_label = user_label
            destination_address = user_address
            if action_type == "video_tip_credit":
                source_address = self._legacy_counterparty_account(public_metadata.get("from_user_id"))
                source_label = "打賞付款帳本身份"
                source_fund_key = None
            elif action_type == "video_tip_platform_fee":
                source_address = self._legacy_counterparty_account(public_metadata.get("from_user_id"))
                source_label = "平台費付款帳本身份"
                source_fund_key = None
                destination_fund_key = "official_treasury"
            if source_fund_key:
                source_label, source_address = self._economy_fund_flow_ref(source_fund_key)
            if destination_fund_key:
                destination_label, destination_address = self._economy_fund_flow_ref(destination_fund_key)
            walletized = bool(source_fund_key or source_address or destination_fund_key)
            return {
                "source_fund_key": source_fund_key,
                "destination_fund_key": destination_fund_key,
                "source_label": source_label or "來源帳本身份",
                "source_wallet_address": source_address,
                "destination_label": destination_label,
                "destination_wallet_address": destination_address,
                "target_wallet_address": "",
                "legacy_public_account_id": legacy_account,
                "walletized": walletized,
                "walletization_note": "legacy row without immutable wallet-flow snapshot",
            }
        if direction in {"debit", "transfer_out", "reverse"}:
            destination_address = ""
            destination_label = ""
            destination_fund_key = self._points_ledger_debit_destination_fund(action_type)
            if action_type == "video_tip_debit":
                destination_address = self._legacy_counterparty_account(public_metadata.get("to_user_id"))
                destination_label = "打賞收款帳本身份"
                return {
                    "source_fund_key": None,
                    "destination_fund_key": None,
                    "source_label": user_label,
                    "source_wallet_address": user_address,
                    "destination_label": destination_label or "打賞收款帳本身份",
                    "destination_wallet_address": destination_address,
                    "target_wallet_address": "",
                    "legacy_public_account_id": legacy_account,
                    "internal_movement": True,
                    "walletized": True,
                    "walletization_note": "影音投幣總額扣款相容列；鏈上資金流由創作者淨收入與官方抽成列拆帳",
                }
            if destination_fund_key:
                destination_label, destination_address = self._economy_fund_flow_ref(destination_fund_key)
            walletized = bool(destination_fund_key or destination_address)
            return {
                "source_fund_key": None,
                "destination_fund_key": destination_fund_key,
                "source_label": user_label,
                "source_wallet_address": user_address,
                "destination_label": destination_label or "目的帳本身份",
                "destination_wallet_address": destination_address,
                "target_wallet_address": "",
                "legacy_public_account_id": legacy_account,
                "walletized": walletized,
                "walletization_note": "legacy row without immutable wallet-flow snapshot",
            }
        if direction in {"freeze", "unfreeze"}:
            return {
                "source_fund_key": None,
                "destination_fund_key": None,
                "source_label": user_label,
                "source_wallet_address": user_address,
                "destination_label": user_label,
                "destination_wallet_address": user_address,
                "target_wallet_address": "",
                "legacy_public_account_id": legacy_account,
                "internal_movement": True,
                "walletized": True,
                "walletization_note": "legacy row without immutable wallet-flow snapshot",
            }
        return {
            "source_fund_key": None,
            "destination_fund_key": None,
            "target_wallet_address": "",
            "legacy_public_account_id": legacy_account,
            "walletized": False,
            "walletization_note": "legacy row without immutable wallet-flow snapshot",
        }

    def _economy_fund_flow_ref(self, fund_key):
        labels = {
            "mint": "MINT 發行錢包",
            "burn": "BURN 銷毀錢包",
            "official_treasury": "官方 Treasury 錢包",
            "promo_fund": "PROMO 獎勵基金",
            "exchange_fund": "EXCHANGE 交易所基金",
        }
        return labels.get(fund_key, fund_key or "-"), economy_fund_address(self.chain_secret, fund_key)

    def _counterparty_user_address(self, conn, value):
        try:
            user_id = int(value)
        except Exception:
            return ""
        address, _label, _target, legacy = self._wallet_address_for_user_flow(conn, user_id)
        return address or legacy

    def _points_ledger_credit_source_fund(self, action_type):
        action = str(action_type or "")
        promo_actions = {
            "daily_login",
            "forum_post_reward",
            "forum_comment_reward",
            "content_like_reward",
            "quality_post_bonus",
            "bug_bounty_low",
            "bug_bounty_medium",
            "bug_bounty_high",
            "bug_bounty_critical",
            "new_user_signup_bonus",
            "user_initial_grant",
            "admin_initial_grant",
            "birthday_gift",
            "game_daily_quest",
            "game_daily_challenge_reward",
            "game_weekly_leaderboard_reward",
            "trading_bot_weekly_competition_reward",
            "reward_thread_author",
        }
        official_actions = {
            "admin_adjust_credit",
            "admin_weekly_salary",
            "official_wallet_grant",
        }
        if action in promo_actions or action.startswith("reward_") or action.startswith("valid_bug_report_") or action.startswith("bug_bounty_"):
            return "promo_fund"
        if action in official_actions:
            return "official_treasury"
        return None

    def _is_configured_auto_distribution_action(self, action_type):
        action = str(action_type or "")
        configured_auto_actions = {
            "daily_login",
            "forum_post_reward",
            "forum_comment_reward",
            "content_like_reward",
            "quality_post_bonus",
            "bug_bounty_low",
            "bug_bounty_medium",
            "bug_bounty_high",
            "bug_bounty_critical",
            "new_user_signup_bonus",
            "user_initial_grant",
            "admin_initial_grant",
            "birthday_gift",
            "game_daily_quest",
            "game_daily_challenge_reward",
            "game_weekly_leaderboard_reward",
            "trading_bot_weekly_competition_reward",
            "reward_thread_author",
            "admin_weekly_salary",
        }
        return action in configured_auto_actions or action.startswith("reward_") or action.startswith("valid_bug_report_") or action.startswith("bug_bounty_")

    def _explorer_chain_fee_policy(self, row):
        action_type = str(row["action_type"] or "")
        try:
            raw_public_metadata = row["public_metadata_json"]
        except Exception:
            raw_public_metadata = row.get("public_metadata_json") if hasattr(row, "get") else None
        if raw_public_metadata is None and hasattr(row, "get"):
            raw_public_metadata = row.get("public_metadata")
        public_metadata = raw_public_metadata if isinstance(raw_public_metadata, dict) else _json_loads(raw_public_metadata, {})
        snapshot = public_metadata.get("wallet_flow_snapshot") if isinstance(public_metadata, dict) else None
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        settlement_rail = str(public_metadata.get("settlement_rail") or snapshot.get("settlement_rail") or "").strip()
        chain_required_value = public_metadata.get("chain_required", snapshot.get("chain_required"))
        chain_required_known = chain_required_value is not None
        chain_required = _metadata_bool(chain_required_value, default=True) if chain_required_known else True
        source_address = str(snapshot.get("source_wallet_address") or public_metadata.get("source_wallet_address") or "").strip().lower()
        destination_address = str(snapshot.get("destination_wallet_address") or public_metadata.get("destination_wallet_address") or "").strip().lower()
        pc0_to_pc0 = bool(
            source_address
            and destination_address
            and is_pc0_internal_address(source_address)
            and is_pc0_internal_address(destination_address)
        )
        if settlement_rail in {"internal_hot_wallet", "internal_system_burn", "deposit_bridge_credit", "withdrawal_bridge_refund"} or (chain_required_known and not chain_required) or pc0_to_pc0:
            return {
                "base_fee_exempt": True,
                "base_fee_destination_fund_key": "burn",
                "base_fee_destination_label": "BURN 銷毀錢包",
                "acceleration_allowed": False,
                "exemption_reason": EXPLORER_INTERNAL_HOT_WALLET_HUMAN_RULE,
                "manual_official_wallet_ops_are_auto": False,
            }
        service_fee_layer = (
            public_metadata.get("settlement_layer") == "service_fee_subledger"
            or action_type.startswith("service_fee_reserve:")
            or action_type in {"service_fee_batch_unfreeze", "service_fee_batch_debit"}
        )
        if service_fee_layer:
            return {
                "base_fee_exempt": True,
                "base_fee_destination_fund_key": "burn",
                "base_fee_destination_label": "BURN 銷毀錢包",
                "acceleration_allowed": False,
                "exemption_reason": "舊版站內服務費 reserve/batch 交易，僅保留歷史相容；新服務費使用 pc0 即時內部扣款",
                "manual_official_wallet_ops_are_auto": False,
            }
        if action_type == "wallet_creation_fee":
            return {
                "base_fee_exempt": True,
                "base_fee_destination_fund_key": "burn",
                "base_fee_destination_label": "BURN 銷毀錢包",
                "acceleration_allowed": False,
                "exemption_reason": "錢包建立功能服務費已入官方 Treasury，單筆不再收鏈上 fee",
                "manual_official_wallet_ops_are_auto": False,
            }
        fee_exempt = self._is_configured_auto_distribution_action(action_type)
        return {
            "base_fee_exempt": fee_exempt,
            "base_fee_destination_fund_key": "burn",
            "base_fee_destination_label": "BURN 銷毀錢包",
            "acceleration_allowed": not fee_exempt,
            "exemption_reason": "設定自動發放交易免鏈上費用" if fee_exempt else "",
            "manual_official_wallet_ops_are_auto": False,
        }

    def _points_ledger_debit_destination_fund(self, action_type):
        action = str(action_type or "")
        if action in {"video_tip_debit"}:
            return None
        if action in {"wallet_transfer_fee"}:
            return "burn"
        if action in {"wallet_creation_fee"}:
            return "official_treasury"
        if action in {"service_fee_batch_debit"}:
            return SERVICE_FEE_REVENUE_DESTINATION_FUND
        if action.startswith("service_fee_internal_debit:"):
            return SERVICE_FEE_REVENUE_DESTINATION_FUND
        if action.startswith("spend:") or action in {"video_boost_debit", "admin_adjust_debit", "chain_acceleration_fee"}:
            return "burn"
        if action.startswith("rollback:"):
            return "burn"
        return None

    def _ledger_wallet_flow_descriptor(self, conn, *, user_id, direction, action_type, public_metadata=None):
        public_metadata = public_metadata if isinstance(public_metadata, dict) else {}
        source_override = str(public_metadata.get("source_wallet_address") or "").strip().lower()
        destination_override = str(public_metadata.get("destination_wallet_address") or "").strip().lower()
        source_fund_override = str(public_metadata.get("source_fund_key") or "").strip().lower()
        destination_fund_override = str(public_metadata.get("destination_fund_key") or "").strip().lower()
        preferred_user_address = destination_override if direction in {"credit", "transfer_in"} else source_override
        user_address, user_label, target_address, legacy_account = self._wallet_address_for_user_flow(conn, user_id, preferred_user_address)
        direction = str(direction or "")
        action_type = str(action_type or "")
        if direction in {"credit", "transfer_in"}:
            source_address = ""
            source_label = ""
            source_fund_key = self._points_ledger_credit_source_fund(action_type)
            destination_fund_key = None
            if source_fund_override:
                source_fund_key = source_fund_override
                source_override = ""
            if destination_fund_override:
                destination_fund_key = destination_fund_override
                destination_override = ""
            destination_label = user_label
            destination_address = user_address
            if action_type == "video_tip_credit":
                source_address = self._counterparty_user_address(conn, public_metadata.get("from_user_id"))
                source_label = "打賞付款錢包"
                source_fund_key = None
            elif action_type == "video_tip_platform_fee":
                source_address = self._counterparty_user_address(conn, public_metadata.get("from_user_id"))
                source_label = "平台費付款錢包"
                source_fund_key = None
                destination_fund_key = "official_treasury"
            elif action_type == "wallet_transfer_in":
                source_address = source_override
                source_label = "轉帳付款錢包"
                source_fund_key = None
            elif source_override:
                source_address = source_override
                source_label = str(public_metadata.get("source_wallet_label") or "來源錢包")
                source_fund_key = None
            if source_fund_key:
                source_label, source_address = self._economy_fund_flow_ref(source_fund_key)
            if destination_fund_key:
                destination_label, destination_address = self._economy_fund_flow_ref(destination_fund_key)
            walletized = bool(source_fund_key or source_address or destination_fund_key)
            return {
                "source_fund_key": source_fund_key,
                "destination_fund_key": destination_fund_key,
                "source_label": source_label or "來源錢包",
                "source_wallet_address": source_address,
                "destination_label": destination_label,
                "destination_wallet_address": destination_address,
                "target_wallet_address": target_address,
                "legacy_public_account_id": legacy_account,
                "walletized": walletized,
                "walletization_note": "" if walletized else "未分類舊帳本收入不再自動套用基金來源",
            }
        if direction in {"debit", "transfer_out", "reverse"}:
            destination_address = ""
            destination_label = ""
            destination_fund_key = self._points_ledger_debit_destination_fund(action_type)
            if destination_fund_override:
                destination_fund_key = destination_fund_override
                destination_override = ""
            if action_type == "video_tip_debit":
                destination_address = self._counterparty_user_address(conn, public_metadata.get("to_user_id"))
                destination_label = "打賞收款錢包"
                return {
                    "source_fund_key": None,
                    "destination_fund_key": None,
                    "source_label": user_label,
                    "source_wallet_address": user_address,
                    "destination_label": destination_label or "打賞收款錢包",
                    "destination_wallet_address": destination_address,
                    "target_wallet_address": target_address,
                    "legacy_public_account_id": legacy_account,
                    "internal_movement": True,
                    "walletized": True,
                    "walletization_note": "影音投幣總額扣款相容列；鏈上資金流由創作者淨收入與官方抽成列拆帳",
                }
            elif action_type == "wallet_transfer_out":
                destination_address = destination_override
                destination_label = "轉帳收款錢包"
            if destination_fund_key:
                destination_label, destination_address = self._economy_fund_flow_ref(destination_fund_key)
            walletized = bool(destination_fund_key or destination_address)
            return {
                "source_fund_key": None,
                "destination_fund_key": destination_fund_key,
                "source_label": user_label,
                "source_wallet_address": user_address,
                "destination_label": destination_label or "目的錢包",
                "destination_wallet_address": destination_address,
                "target_wallet_address": target_address,
                "legacy_public_account_id": legacy_account,
                "walletized": walletized,
                "walletization_note": "" if walletized else "未分類舊帳本支出不再自動套用基金目的地",
            }
        if direction in {"freeze", "unfreeze"}:
            return {
                "source_fund_key": None,
                "destination_fund_key": None,
                "source_label": user_label,
                "source_wallet_address": user_address,
                "destination_label": user_label,
                "destination_wallet_address": user_address,
                "target_wallet_address": target_address,
                "legacy_public_account_id": legacy_account,
                "internal_movement": True,
                "walletized": True,
            }
        return {
            "source_fund_key": None,
            "destination_fund_key": None,
            "target_wallet_address": target_address,
            "legacy_public_account_id": legacy_account,
            "walletized": False,
        }

    def _wallet_flow_snapshot_for_ledger_write(self, conn, *, user_id, direction, action_type, public_metadata=None):
        metadata = public_metadata if isinstance(public_metadata, dict) else {}
        flow = self._ledger_wallet_flow_descriptor(
            conn,
            user_id=user_id,
            direction=direction,
            action_type=action_type,
            public_metadata=metadata,
        )
        settlement_rail = str(metadata.get("settlement_rail") or "").strip()
        if settlement_rail:
            flow["settlement_rail"] = settlement_rail
            flow["chain_required"] = _metadata_bool(metadata.get("chain_required"), default=True)
            flow["approval_required"] = _metadata_bool(metadata.get("approval_required"), default=True)
            flow["network_fee_points"] = int(metadata.get("network_fee_points") or 0)
            flow["service_fee_points"] = int(metadata.get("service_fee_points") or 0)
            if (
                settlement_rail in {"internal_hot_wallet", "deposit_bridge_credit", "withdrawal_bridge_refund"}
                and not flow.get("source_fund_key")
                and not flow.get("destination_fund_key")
            ):
                flow["internal_movement"] = True
        snapshot = {key: value for key, value in flow.items() if isinstance(value, (str, int, float, bool)) or value is None}
        snapshot["snapshot_source"] = "ledger_write"
        snapshot["snapshot_version"] = 1
        return snapshot

    def _ledger_metadata_with_wallet_flow_snapshot(self, conn, *, user_id, direction, action_type, public_metadata=None):
        metadata = dict(public_metadata or {})
        metadata.pop("wallet_flow_snapshot", None)
        snapshot = self._wallet_flow_snapshot_for_ledger_write(
            conn,
            user_id=user_id,
            direction=direction,
            action_type=action_type,
            public_metadata=metadata,
        )
        metadata["wallet_flow_snapshot"] = snapshot
        return metadata, snapshot

    def _pending_transfer_outgoing_by_address(self, conn, addresses, *, exclude_request_uuid=None, branch_uuid=None):
        normalized = sorted({str(address or "").strip().lower() for address in addresses or [] if address})
        if not normalized:
            return {}
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        placeholders = ", ".join("?" for _ in normalized)
        sql = f"""
            SELECT source_wallet_address, COALESCE(SUM(amount_points + fee_points), 0) AS pending_total
            FROM points_chain_transfer_requests
            WHERE status='pending' AND chain_branch=? AND source_wallet_address IN ({placeholders})
        """
        params = [branch, *normalized]
        if exclude_request_uuid:
            sql += " AND request_uuid<>?"
            params.append(str(exclude_request_uuid))
        sql += " GROUP BY source_wallet_address"
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        except Exception:
            return {}
        return {str(row["source_wallet_address"] or ""): int(row["pending_total"] or 0) for row in rows}

    def _wallet_identity_balance_hash(self, *, branch, wallet_address, user_id, wallet_identity_id, available, frozen, pending, last_ledger_id, last_transfer_request_id, last_bridge_event_id):
        return sha256_text(canonical_json({
            "version": "points_wallet_identity_balance_v1",
            "chain_branch": str(branch or ""),
            "wallet_address": str(wallet_address or "").strip().lower(),
            "user_id": int(user_id or 0),
            "wallet_identity_id": int(wallet_identity_id or 0) if wallet_identity_id is not None else None,
            "available_points": int(available or 0),
            "frozen_points": int(frozen or 0),
            "pending_outgoing_points": int(pending or 0),
            "last_ledger_id": int(last_ledger_id or 0),
            "last_transfer_request_id": int(last_transfer_request_id or 0),
            "last_bridge_event_id": int(last_bridge_event_id or 0),
        }))

    def _wallet_identity_materialized_registry_locked(self, conn, *, branch=None):
        if not table_columns(conn, "points_wallet_identities"):
            return {}
        branch = str(branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        registry = {}
        rows = conn.execute(
            """
            SELECT id AS wallet_identity_id, user_id, LOWER(address) AS wallet_address,
                   wallet_type, custody_mode, is_primary, status
            FROM points_wallet_identities
            WHERE user_id IS NOT NULL
              AND status IN ('pending_backup', 'active')
            """
        ).fetchall()
        for row in rows:
            address = str(row["wallet_address"] or "").strip().lower()
            if not address:
                continue
            registry.setdefault(address, {
                "chain_branch": branch,
                "wallet_address": address,
                "user_id": int(row["user_id"] or 0),
                "wallet_identity_id": int(row["wallet_identity_id"] or 0),
                "wallet_type": str(row["wallet_type"] or ""),
                "custody_mode": str(row["custody_mode"] or ""),
                "available_points": 0,
                "frozen_points": 0,
                "pending_outgoing_points": 0,
                "last_ledger_id": 0,
                "last_transfer_request_id": 0,
                "last_bridge_event_id": 0,
            })
        if table_columns(conn, "points_wallet_identity_bindings"):
            binding_rows = conn.execute(
                """
                SELECT w.id AS wallet_identity_id,
                       b.user_id AS binding_user_id,
                       LOWER(COALESCE(NULLIF(b.address, ''), w.address)) AS wallet_address,
                       COALESCE(NULLIF(b.wallet_type, ''), w.wallet_type) AS wallet_type,
                       w.custody_mode
                FROM points_wallet_identity_bindings b
                JOIN points_wallet_identities w ON w.id=b.wallet_identity_id
                WHERE b.status='active'
                  AND w.status IN ('pending_backup', 'active')
                """
            ).fetchall()
            for row in binding_rows:
                address = str(row["wallet_address"] or "").strip().lower()
                if not address or address in registry:
                    continue
                registry[address] = {
                    "chain_branch": branch,
                    "wallet_address": address,
                    "user_id": int(row["binding_user_id"] or 0),
                    "wallet_identity_id": int(row["wallet_identity_id"] or 0),
                    "wallet_type": str(row["wallet_type"] or ""),
                    "custody_mode": str(row["custody_mode"] or ""),
                    "available_points": 0,
                    "frozen_points": 0,
                    "pending_outgoing_points": 0,
                    "last_ledger_id": 0,
                    "last_transfer_request_id": 0,
                    "last_bridge_event_id": 0,
                }
        return registry

    def rebuild_wallet_identity_balances(self, conn, chain_branch=None):
        branch = str(chain_branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        balances = self._wallet_identity_materialized_registry_locked(conn, branch=branch)
        last_ledger_id = 0
        last_ledger_hash = ""
        rows = conn.execute(
            """
            SELECT *
            FROM points_ledger
            WHERE status='confirmed' AND chain_branch=?
            ORDER BY id ASC
            """,
            (branch,),
        ).fetchall()
        for row in rows:
            last_ledger_id = max(last_ledger_id, int(row["id"] or 0))
            last_ledger_hash = str(row["ledger_hash"] or last_ledger_hash)
            flow = self._ledger_wallet_flow_for_read(conn, row)
            amount = int(row["amount"] or 0)
            direction = str(row["direction"] or "")
            if direction in {"credit", "transfer_in"}:
                address = str(flow.get("destination_wallet_address") or "").strip().lower()
                if address in balances:
                    balances[address]["available_points"] += amount
                    balances[address]["last_ledger_id"] = last_ledger_id
            elif direction in {"debit", "transfer_out", "reverse"}:
                address = str(flow.get("source_wallet_address") or "").strip().lower()
                if address in balances:
                    balances[address]["available_points"] -= amount
                    balances[address]["last_ledger_id"] = last_ledger_id
            elif direction == "freeze":
                address = str(flow.get("source_wallet_address") or "").strip().lower()
                if address in balances:
                    balances[address]["available_points"] -= amount
                    balances[address]["frozen_points"] += amount
                    balances[address]["last_ledger_id"] = last_ledger_id
            elif direction == "unfreeze":
                address = str(flow.get("source_wallet_address") or flow.get("destination_wallet_address") or "").strip().lower()
                if address in balances:
                    balances[address]["available_points"] += amount
                    balances[address]["frozen_points"] -= amount
                    balances[address]["last_ledger_id"] = last_ledger_id
        if balances:
            placeholders = ", ".join("?" for _ in balances)
            transfer_rows = conn.execute(
                f"""
                SELECT destination_wallet_address, COALESCE(SUM(amount_points), 0) AS received
                FROM points_chain_transfer_requests
                WHERE status='confirmed'
                  AND chain_branch=?
                  AND destination_wallet_address IN ({placeholders})
                  AND (transfer_in_ledger_uuid IS NULL OR transfer_in_ledger_uuid='')
                GROUP BY destination_wallet_address
                """,
                (branch, *tuple(balances.keys())),
            ).fetchall()
            for row in transfer_rows:
                address = str(row["destination_wallet_address"] or "").strip().lower()
                if address in balances:
                    balances[address]["available_points"] += int(row["received"] or 0)
        last_transfer_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS id FROM points_chain_transfer_requests WHERE chain_branch=?",
            (branch,),
        ).fetchone()
        last_transfer_id = int((last_transfer_row["id"] if last_transfer_row else 0) or 0)
        pending_by_address = self._pending_transfer_outgoing_by_address(conn, balances.keys(), branch_uuid=branch)
        for address, pending in pending_by_address.items():
            if address in balances:
                balances[address]["pending_outgoing_points"] = int(pending or 0)
                balances[address]["last_transfer_request_id"] = last_transfer_id
        bridge_row = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM points_chain_bridge_events").fetchone()
        last_bridge_id = int((bridge_row["id"] if bridge_row else 0) or 0)
        now = utc_now()
        for item in balances.values():
            if int(item["available_points"] or 0) < 0 or int(item["frozen_points"] or 0) < 0 or int(item["pending_outgoing_points"] or 0) < 0:
                raise ValueError(f"wallet identity replay would create negative balance for {item['wallet_address']}")
            item["last_ledger_id"] = max(int(item.get("last_ledger_id") or 0), last_ledger_id)
            item["last_transfer_request_id"] = max(int(item.get("last_transfer_request_id") or 0), last_transfer_id)
            item["last_bridge_event_id"] = last_bridge_id
            item["balance_hash"] = self._wallet_identity_balance_hash(
                branch=branch,
                wallet_address=item["wallet_address"],
                user_id=item["user_id"],
                wallet_identity_id=item["wallet_identity_id"],
                available=item["available_points"],
                frozen=item["frozen_points"],
                pending=item["pending_outgoing_points"],
                last_ledger_id=item["last_ledger_id"],
                last_transfer_request_id=item["last_transfer_request_id"],
                last_bridge_event_id=item["last_bridge_event_id"],
            )
        conn.execute("DELETE FROM points_wallet_identity_balances WHERE chain_branch=?", (branch,))
        for item in balances.values():
            conn.execute(
                """
                INSERT INTO points_wallet_identity_balances (
                    chain_branch, wallet_address, user_id, wallet_identity_id, wallet_type, custody_mode,
                    available_points, frozen_points, pending_outgoing_points,
                    last_ledger_id, last_transfer_request_id, last_bridge_event_id,
                    balance_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    branch,
                    item["wallet_address"],
                    int(item["user_id"] or 0),
                    item.get("wallet_identity_id"),
                    item.get("wallet_type") or "",
                    item.get("custody_mode") or "",
                    int(item["available_points"] or 0),
                    int(item["frozen_points"] or 0),
                    int(item["pending_outgoing_points"] or 0),
                    int(item["last_ledger_id"] or 0),
                    int(item["last_transfer_request_id"] or 0),
                    int(item["last_bridge_event_id"] or 0),
                    item["balance_hash"],
                    now,
                ),
            )
        root_hash = merkle_root([item["balance_hash"] for item in sorted(balances.values(), key=lambda value: value["wallet_address"])])
        conn.execute(
            """
            INSERT INTO points_wallet_identity_balance_state (
                chain_branch, replay_height, last_ledger_hash, last_transfer_request_id,
                last_bridge_event_id, wallet_root_hash, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain_branch) DO UPDATE SET
                replay_height=excluded.replay_height,
                last_ledger_hash=excluded.last_ledger_hash,
                last_transfer_request_id=excluded.last_transfer_request_id,
                last_bridge_event_id=excluded.last_bridge_event_id,
                wallet_root_hash=excluded.wallet_root_hash,
                updated_at=excluded.updated_at
            """,
            (branch, last_ledger_id, last_ledger_hash, last_transfer_id, last_bridge_id, root_hash, now),
        )
        return {
            "ok": True,
            "chain_branch": branch,
            "wallet_count": len(balances),
            "replay_height": last_ledger_id,
            "last_transfer_request_id": last_transfer_id,
            "last_bridge_event_id": last_bridge_id,
            "wallet_root_hash": root_hash,
        }

    def verify_wallet_identity_balances(self, conn, chain_branch=None, *, mode="full"):
        branch = str(chain_branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        snapshot = {}
        for row in conn.execute(
            "SELECT * FROM points_wallet_identity_balances WHERE chain_branch=?",
            (branch,),
        ).fetchall():
            snapshot[str(row["wallet_address"] or "").strip().lower()] = dict(row)
        savepoint = f"sp_verify_wallet_identity_balances_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            rebuilt = self.rebuild_wallet_identity_balances(conn, branch)
            expected = {
                str(row["wallet_address"] or "").strip().lower(): dict(row)
                for row in conn.execute(
                    "SELECT * FROM points_wallet_identity_balances WHERE chain_branch=?",
                    (branch,),
                ).fetchall()
            }
            conn.execute(f"ROLLBACK TO {savepoint}")
        finally:
            conn.execute(f"RELEASE {savepoint}")
        errors = []
        all_addresses = sorted(set(snapshot) | set(expected))
        for address in all_addresses:
            actual = snapshot.get(address)
            want = expected.get(address)
            if not actual:
                errors.append({"type": "wallet_identity_balance_missing", "wallet_address": address})
                continue
            if not want:
                errors.append({"type": "wallet_identity_balance_orphan", "wallet_address": address})
                continue
            mismatches = []
            for column in ("available_points", "frozen_points", "pending_outgoing_points"):
                if int(actual.get(column) or 0) != int(want.get(column) or 0):
                    mismatches.append({
                        "field": column,
                        "expected": int(want.get(column) or 0),
                        "actual": int(actual.get(column) or 0),
                    })
            if str(actual.get("balance_hash") or "") != str(want.get("balance_hash") or ""):
                mismatches.append({
                    "field": "balance_hash",
                    "expected": str(want.get("balance_hash") or ""),
                    "actual": str(actual.get("balance_hash") or ""),
                })
            if mismatches:
                errors.append({
                    "type": "wallet_identity_balance_mismatch",
                    "wallet_address": address,
                    "mismatches": mismatches,
                })
        state = conn.execute(
            "SELECT * FROM points_wallet_identity_balance_state WHERE chain_branch=?",
            (branch,),
        ).fetchone()
        if not state:
            errors.append({"type": "wallet_identity_balance_state_missing", "chain_branch": branch})
        return {
            "ok": not errors,
            "chain_branch": branch,
            "mode": str(mode or "full"),
            "errors": errors[:100],
            "error_count": len(errors),
            "expected": rebuilt,
            "wallet_count": len(snapshot),
        }

    def _wallet_identity_materialized_state_valid_locked(self, conn, *, branch):
        state = conn.execute(
            "SELECT * FROM points_wallet_identity_balance_state WHERE chain_branch=?",
            (branch,),
        ).fetchone()
        if not state:
            return False
        ledger_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS id FROM points_ledger WHERE status='confirmed' AND chain_branch=?",
            (branch,),
        ).fetchone()
        latest_ledger_id = int((ledger_row["id"] if ledger_row else 0) or 0)
        return int(state["replay_height"] or 0) >= latest_ledger_id

    def _wallet_identity_materialized_rows_for_user_locked(self, conn, *, user_id, branch, addresses):
        normalized = sorted({str(address or "").strip().lower() for address in addresses or [] if address})
        if not normalized:
            return {}
        started_transaction = not conn.in_transaction
        if not self._wallet_identity_materialized_state_valid_locked(conn, branch=branch):
            self.rebuild_wallet_identity_balances(conn, branch)
            if started_transaction:
                conn.commit()
        placeholders = ", ".join("?" for _ in normalized)
        rows = conn.execute(
            f"""
            SELECT *
            FROM points_wallet_identity_balances
            WHERE chain_branch=? AND wallet_address IN ({placeholders})
            """,
            (branch, *normalized),
        ).fetchall()
        by_address = {str(row["wallet_address"] or "").strip().lower(): row for row in rows}
        if set(by_address) != set(normalized):
            self.rebuild_wallet_identity_balances(conn, branch)
            if started_transaction:
                conn.commit()
            rows = conn.execute(
                f"""
                SELECT *
                FROM points_wallet_identity_balances
                WHERE chain_branch=? AND wallet_address IN ({placeholders})
                """,
                (branch, *normalized),
            ).fetchall()
            by_address = {str(row["wallet_address"] or "").strip().lower(): row for row in rows}
        return by_address

    def _refresh_wallet_identity_pending_for_address_locked(self, conn, address, *, branch=None):
        address = str(address or "").strip().lower()
        if not address:
            return
        branch = str(branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        row = conn.execute(
            "SELECT * FROM points_wallet_identity_balances WHERE chain_branch=? AND wallet_address=?",
            (branch, address),
        ).fetchone()
        if not row:
            return
        pending = self._pending_transfer_outgoing_for_address(conn, address, branch_uuid=branch)
        transfer_row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS id FROM points_chain_transfer_requests WHERE chain_branch=?",
            (branch,),
        ).fetchone()
        last_transfer_id = int((transfer_row["id"] if transfer_row else 0) or 0)
        balance_hash = self._wallet_identity_balance_hash(
            branch=branch,
            wallet_address=address,
            user_id=int(row["user_id"] or 0),
            wallet_identity_id=row["wallet_identity_id"],
            available=int(row["available_points"] or 0),
            frozen=int(row["frozen_points"] or 0),
            pending=pending,
            last_ledger_id=int(row["last_ledger_id"] or 0),
            last_transfer_request_id=last_transfer_id,
            last_bridge_event_id=int(row["last_bridge_event_id"] or 0),
        )
        conn.execute(
            """
            UPDATE points_wallet_identity_balances
            SET pending_outgoing_points=?, last_transfer_request_id=?, balance_hash=?, updated_at=?
            WHERE chain_branch=? AND wallet_address=?
            """,
            (pending, last_transfer_id, balance_hash, utc_now(), branch, address),
        )

    def _apply_wallet_identity_materialized_ledger_delta_locked(self, conn, *, ledger_row, wallet_flow_snapshot):
        if not table_columns(conn, "points_wallet_identity_balances"):
            return
        branch = str(ledger_row["chain_branch"] or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        amount = int(ledger_row["amount"] or 0)
        direction = str(ledger_row["direction"] or "")
        if direction in {"credit", "transfer_in"}:
            address = str(wallet_flow_snapshot.get("destination_wallet_address") or "").strip().lower()
            available_delta, frozen_delta = amount, 0
        elif direction in {"debit", "transfer_out", "reverse"}:
            address = str(wallet_flow_snapshot.get("source_wallet_address") or "").strip().lower()
            available_delta, frozen_delta = -amount, 0
        elif direction == "freeze":
            address = str(wallet_flow_snapshot.get("source_wallet_address") or "").strip().lower()
            available_delta, frozen_delta = -amount, amount
        elif direction == "unfreeze":
            address = str(wallet_flow_snapshot.get("source_wallet_address") or wallet_flow_snapshot.get("destination_wallet_address") or "").strip().lower()
            available_delta, frozen_delta = amount, -amount
        else:
            return
        if not address:
            return
        row = conn.execute(
            "SELECT * FROM points_wallet_identity_balances WHERE chain_branch=? AND wallet_address=?",
            (branch, address),
        ).fetchone()
        if not row:
            self.rebuild_wallet_identity_balances(conn, branch)
            row = conn.execute(
                "SELECT * FROM points_wallet_identity_balances WHERE chain_branch=? AND wallet_address=?",
                (branch, address),
            ).fetchone()
            if not row:
                return
        available = int(row["available_points"] or 0) + int(available_delta or 0)
        frozen = int(row["frozen_points"] or 0) + int(frozen_delta or 0)
        if available < 0 or frozen < 0:
            raise ValueError(f"wallet identity materialized balance would become negative for {address}")
        pending = self._pending_transfer_outgoing_for_address(conn, address, branch_uuid=branch)
        last_ledger_id = int(ledger_row["id"] or 0)
        balance_hash = self._wallet_identity_balance_hash(
            branch=branch,
            wallet_address=address,
            user_id=int(row["user_id"] or 0),
            wallet_identity_id=row["wallet_identity_id"],
            available=available,
            frozen=frozen,
            pending=pending,
            last_ledger_id=last_ledger_id,
            last_transfer_request_id=int(row["last_transfer_request_id"] or 0),
            last_bridge_event_id=int(row["last_bridge_event_id"] or 0),
        )
        now = utc_now()
        conn.execute(
            """
            UPDATE points_wallet_identity_balances
            SET available_points=?, frozen_points=?, pending_outgoing_points=?,
                last_ledger_id=?, balance_hash=?, updated_at=?
            WHERE chain_branch=? AND wallet_address=?
            """,
            (available, frozen, pending, last_ledger_id, balance_hash, now, branch, address),
        )
        state = conn.execute(
            "SELECT * FROM points_wallet_identity_balance_state WHERE chain_branch=?",
            (branch,),
        ).fetchone()
        if state:
            conn.execute(
                """
                UPDATE points_wallet_identity_balance_state
                SET replay_height=MAX(replay_height, ?), last_ledger_hash=?, updated_at=?
                WHERE chain_branch=?
                """,
                (last_ledger_id, ledger_row["ledger_hash"], now, branch),
            )

    def _wallet_identity_balances_for_user(
        self,
        conn,
        user_id,
        *,
        include_pending=True,
        exclude_request_uuid=None,
        branch_uuid=None,
        account_statement=None,
    ):
        state = self._wallet_identity_state_for_user(conn, user_id)
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        balances = {}
        if not state["has_identity"]:
            return {"has_identity": False, "primary_address": "", "balances": balances}
        for address in state["addresses"]:
            balances[address] = {"balance": 0, "frozen": 0, "pending_outgoing": 0}
        try:
            materialized_rows = self._wallet_identity_materialized_rows_for_user_locked(
                conn,
                user_id=int(user_id),
                branch=branch,
                addresses=balances.keys(),
            )
        except Exception:
            materialized_rows = {}
        if materialized_rows and set(materialized_rows) == set(balances):
            materialized_balances = {}
            for address, row in materialized_rows.items():
                materialized_balances[address] = {
                    "balance": int(row["available_points"] or 0),
                    "frozen": int(row["frozen_points"] or 0),
                    "pending_outgoing": 0,
                }
            materialized_matches = True
            if account_statement is not None:
                materialized_matches = (
                    sum(int(item.get("balance") or 0) for item in materialized_balances.values()) == int(account_statement.get("balance") or 0)
                    and sum(int(item.get("frozen") or 0) for item in materialized_balances.values()) == int(account_statement.get("frozen") or 0)
                )
            if materialized_matches:
                balances = materialized_balances
                if include_pending:
                    pending_by_address = self._pending_transfer_outgoing_by_address(
                        conn,
                        balances.keys(),
                        exclude_request_uuid=exclude_request_uuid,
                        branch_uuid=branch,
                    )
                    for address, pending in pending_by_address.items():
                        if address in balances and int(pending or 0) > 0:
                            pending = int(pending or 0)
                            balances[address]["balance"] -= pending
                            balances[address]["frozen"] += pending
                            balances[address]["pending_outgoing"] = pending
                primary = state["primary"]
                return {
                    "has_identity": True,
                    "primary_address": primary["address"] if primary else "",
                    "balances": balances,
                    "source": "materialized_wallet_identity_balances",
                }
        can_use_single_wallet_aggregate = (
            len(balances) == 1
            and int(state.get("identity_row_count") or 0) == 1
            and int(state.get("active_identity_count") or 0) == 1
        )
        if can_use_single_wallet_aggregate:
            wallet = conn.execute(
                """
                SELECT soft_balance, hard_balance, soft_frozen, hard_frozen
                FROM points_wallets
                WHERE user_id=?
                """,
                (int(user_id),),
            ).fetchone()
            if wallet:
                address = next(iter(balances))
                aggregate_balance = int(wallet["soft_balance"] or 0) + int(wallet["hard_balance"] or 0)
                aggregate_frozen = int(wallet["soft_frozen"] or 0) + int(wallet["hard_frozen"] or 0)
                statement_matches = True
                if account_statement is not None:
                    statement_matches = (
                        aggregate_balance == int(account_statement.get("balance") or 0)
                        and aggregate_frozen == int(account_statement.get("frozen") or 0)
                    )
                if statement_matches:
                    balances[address]["balance"] = aggregate_balance
                    balances[address]["frozen"] = aggregate_frozen
                    if include_pending:
                        pending_by_address = self._pending_transfer_outgoing_by_address(
                            conn,
                            balances.keys(),
                            exclude_request_uuid=exclude_request_uuid,
                            branch_uuid=branch,
                        )
                        pending = int(pending_by_address.get(address) or 0)
                        if pending > 0:
                            balances[address]["balance"] -= pending
                            balances[address]["frozen"] += pending
                            balances[address]["pending_outgoing"] = pending
                    primary = state["primary"]
                    return {
                        "has_identity": True,
                        "primary_address": primary["address"] if primary else address,
                        "balances": balances,
                    }
        rows = conn.execute(
            """
            SELECT * FROM points_ledger
            WHERE status='confirmed' AND chain_branch=?
            ORDER BY id ASC
            """,
            (branch,),
        ).fetchall()
        for row in rows:
            flow = self._ledger_wallet_flow_for_read(conn, row)
            amount = int(row["amount"] or 0)
            direction = str(row["direction"] or "")
            if direction in {"credit", "transfer_in"}:
                address = str(flow.get("destination_wallet_address") or "")
                if address in balances:
                    balances[address]["balance"] += amount
            elif direction in {"debit", "transfer_out", "reverse"}:
                address = str(flow.get("source_wallet_address") or "")
                if address in balances:
                    balances[address]["balance"] -= amount
            elif direction == "freeze":
                address = str(flow.get("source_wallet_address") or "")
                if address in balances:
                    balances[address]["balance"] -= amount
                    balances[address]["frozen"] += amount
            elif direction == "unfreeze":
                address = str(flow.get("source_wallet_address") or flow.get("destination_wallet_address") or "")
                if address in balances:
                    balances[address]["balance"] += amount
                    balances[address]["frozen"] -= amount
        if balances:
            placeholders = ", ".join("?" for _ in balances)
            transfer_rows = conn.execute(
                f"""
                SELECT destination_wallet_address, COALESCE(SUM(amount_points), 0) AS received
                FROM points_chain_transfer_requests
                WHERE status='confirmed'
                  AND chain_branch=?
                  AND destination_wallet_address IN ({placeholders})
                  AND (transfer_in_ledger_uuid IS NULL OR transfer_in_ledger_uuid='')
                GROUP BY destination_wallet_address
                """,
                (branch, *tuple(balances.keys())),
            ).fetchall()
            for row in transfer_rows:
                address = str(row["destination_wallet_address"] or "")
                if address in balances:
                    balances[address]["balance"] += int(row["received"] or 0)
        if include_pending:
            pending_by_address = self._pending_transfer_outgoing_by_address(
                conn,
                balances.keys(),
                exclude_request_uuid=exclude_request_uuid,
                branch_uuid=branch,
            )
            for address, pending in pending_by_address.items():
                if address in balances and pending > 0:
                    balances[address]["balance"] -= pending
                    balances[address]["frozen"] += pending
                    balances[address]["pending_outgoing"] = pending
        primary = state["primary"]
        return {
            "has_identity": True,
            "primary_address": primary["address"] if primary else "",
            "balances": balances,
        }

    def _official_hot_wallet_circulation_totals(self, conn, *, branch_uuid=None):
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        if not table_columns(conn, "points_wallet_identities"):
            return {"wallet_count": 0, "root_wallet_count": 0, "member_wallet_count": 0}
        rows = conn.execute(
            """
            SELECT w.user_id, LOWER(w.address) AS address, COALESCE(LOWER(u.username), '') AS username
            FROM points_wallet_identities w
            LEFT JOIN users u ON u.id=w.user_id
            WHERE w.status='active'
              AND w.wallet_type='official_hot'
              AND LOWER(w.address) LIKE 'pc0%'
            ORDER BY w.user_id, w.id
            """
        ).fetchall()
        addresses = {
            str(row["address"] or "").strip().lower(): {
                "user_id": int(row["user_id"] or 0),
                "username": str(row["username"] or ""),
                "balance": 0,
                "frozen": 0,
                "pending_outgoing": 0,
            }
            for row in rows
            if str(row["address"] or "").strip()
        }
        if not addresses:
            return {"wallet_count": 0, "root_wallet_count": 0, "member_wallet_count": 0}

        ledger_rows = conn.execute(
            """
            SELECT *
            FROM points_ledger
            WHERE status='confirmed' AND chain_branch=?
            ORDER BY id ASC
            """,
            (branch,),
        ).fetchall()
        for row in ledger_rows:
            flow = self._ledger_wallet_flow_for_read(conn, row)
            amount = int(row["amount"] or 0)
            direction = str(row["direction"] or "")
            if direction in {"credit", "transfer_in"}:
                address = str(flow.get("destination_wallet_address") or "").strip().lower()
                if address in addresses:
                    addresses[address]["balance"] += amount
            elif direction in {"debit", "transfer_out", "reverse"}:
                address = str(flow.get("source_wallet_address") or "").strip().lower()
                if address in addresses:
                    addresses[address]["balance"] -= amount
            elif direction == "freeze":
                address = str(flow.get("source_wallet_address") or "").strip().lower()
                if address in addresses:
                    addresses[address]["balance"] -= amount
                    addresses[address]["frozen"] += amount
            elif direction == "unfreeze":
                address = str(flow.get("source_wallet_address") or flow.get("destination_wallet_address") or "").strip().lower()
                if address in addresses:
                    addresses[address]["balance"] += amount
                    addresses[address]["frozen"] -= amount

        placeholders = ", ".join("?" for _ in addresses)
        transfer_rows = conn.execute(
            f"""
            SELECT destination_wallet_address, COALESCE(SUM(amount_points), 0) AS received
            FROM points_chain_transfer_requests
            WHERE status='confirmed'
              AND chain_branch=?
              AND destination_wallet_address IN ({placeholders})
              AND (transfer_in_ledger_uuid IS NULL OR transfer_in_ledger_uuid='')
            GROUP BY destination_wallet_address
            """,
            (branch, *tuple(addresses.keys())),
        ).fetchall()
        for row in transfer_rows:
            address = str(row["destination_wallet_address"] or "").strip().lower()
            if address in addresses:
                addresses[address]["balance"] += int(row["received"] or 0)

        pending_by_address = self._pending_transfer_outgoing_by_address(conn, addresses.keys(), branch_uuid=branch)
        for address, pending in pending_by_address.items():
            address = str(address or "").strip().lower()
            if address in addresses and int(pending or 0) > 0:
                pending = int(pending or 0)
                addresses[address]["balance"] -= pending
                addresses[address]["frozen"] += pending
                addresses[address]["pending_outgoing"] = pending

        member_available = 0
        member_frozen = 0
        root_available = 0
        root_frozen = 0
        member_wallet_count = 0
        root_wallet_count = 0
        for item in addresses.values():
            if item["username"] == "root":
                root_wallet_count += 1
                root_available += int(item["balance"] or 0)
                root_frozen += int(item["frozen"] or 0)
            else:
                member_wallet_count += 1
                member_available += int(item["balance"] or 0)
                member_frozen += int(item["frozen"] or 0)
        return {
            "wallet_count": len(addresses),
            "member_wallet_count": member_wallet_count,
            "root_wallet_count": root_wallet_count,
            "available_points": member_available + root_available,
            "frozen_points": member_frozen + root_frozen,
            "outstanding_points": member_available + member_frozen + root_available + root_frozen,
            "member_available_points": member_available,
            "member_frozen_points": member_frozen,
            "member_outstanding_points": member_available + member_frozen,
            "root_available_points": root_available,
            "root_frozen_points": root_frozen,
            "root_outstanding_points": root_available + root_frozen,
        }

    def _exchange_principal_receivable_breakdown_locked(self, conn, *, branch_uuid=None):
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        identity_by_address = {}
        if table_columns(conn, "points_wallet_identities"):
            rows = conn.execute(
                """
                SELECT LOWER(w.address) AS address,
                       COALESCE(LOWER(u.username), '') AS username
                FROM points_wallet_identities w
                LEFT JOIN users u ON u.id=w.user_id
                ORDER BY CASE WHEN w.status='active' THEN 0 ELSE 1 END, w.id ASC
                """
            ).fetchall()
            for row in rows:
                address = str(row["address"] or "").strip().lower()
                if address and address not in identity_by_address:
                    identity_by_address[address] = str(row["username"] or "")

        totals = {
            "total_points": 0,
            "pc0_member_points": 0,
            "pc0_root_points": 0,
            "pc0_unknown_points": 0,
            "synthetic_receivable_points": 0,
            "other_receivable_points": 0,
            "address_count": 0,
            "sample_addresses": [],
        }
        if not table_columns(conn, "points_economy_events"):
            return totals
        lent_types = set(EXCHANGE_PRINCIPAL_LENT_TYPES)
        repaid_types = set(EXCHANGE_PRINCIPAL_REPAID_TYPES)
        principal_by_address = {}
        placeholders = ", ".join("?" for _ in sorted(lent_types | repaid_types))
        rows = conn.execute(
            f"""
            SELECT transaction_type, source_address, destination_address, amount
            FROM points_economy_events
            WHERE status='confirmed'
              AND chain_branch=?
              AND transaction_type IN ({placeholders})
            ORDER BY id ASC
            """,
            (branch, *sorted(lent_types | repaid_types)),
        ).fetchall()
        for row in rows:
            amount = int(row["amount"] or 0)
            transaction_type = str(row["transaction_type"] or "")
            if transaction_type in lent_types:
                address = str(row["destination_address"] or "").strip().lower()
                direction = 1
            elif transaction_type in repaid_types:
                address = str(row["source_address"] or "").strip().lower()
                direction = -1
            else:
                continue
            if not address:
                continue
            principal_by_address[address] = int(principal_by_address.get(address) or 0) + (amount * direction)
        samples = []
        for address, raw_amount in sorted(principal_by_address.items()):
            amount = int(raw_amount or 0)
            if amount <= 0:
                continue
            if is_pc0_internal_address(address):
                username = identity_by_address.get(address, "")
                if username == "root":
                    bucket = "pc0_root_points"
                elif username:
                    bucket = "pc0_member_points"
                else:
                    bucket = "pc0_unknown_points"
            elif address.startswith(EXCHANGE_PRINCIPAL_RECEIVABLE_ADDRESS_PREFIX):
                bucket = "synthetic_receivable_points"
            else:
                bucket = "other_receivable_points"
            totals[bucket] += amount
            totals["total_points"] += amount
            totals["address_count"] += 1
            if len(samples) < 12:
                samples.append({"address": address, "points": amount, "bucket": bucket})
        totals["sample_addresses"] = samples
        return totals

    def _margin_settlement_withheld_breakdown_locked(self, conn, *, branch_uuid=None):
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        totals = {
            "total_points": 0,
            "pc0_member_points": 0,
            "pc0_root_points": 0,
            "pc0_unknown_points": 0,
            "covered_by_wallet_loss_debit_points": 0,
            "address_count": 0,
            "sample_addresses": [],
        }
        if not table_columns(conn, "points_economy_events"):
            return totals
        identity_by_address = {}
        address_by_user = {}
        if table_columns(conn, "points_wallet_identities"):
            rows = conn.execute(
                """
                SELECT LOWER(w.address) AS address,
                       w.user_id AS user_id,
                       COALESCE(LOWER(u.username), '') AS username
                FROM points_wallet_identities w
                LEFT JOIN users u ON u.id=w.user_id
                WHERE w.wallet_type='official_hot'
                ORDER BY CASE WHEN w.status='active' THEN 0 ELSE 1 END, w.id ASC
                """
            ).fetchall()
            for row in rows:
                address = str(row["address"] or "").strip().lower()
                if not address:
                    continue
                if address not in identity_by_address:
                    identity_by_address[address] = str(row["username"] or "")
                try:
                    user_id = int(row["user_id"] or 0)
                except (TypeError, ValueError):
                    user_id = 0
                if user_id and user_id not in address_by_user:
                    address_by_user[user_id] = address

        retained_by_address = {}
        loss_collected_by_address = {}
        rows = conn.execute(
            """
            SELECT transaction_type, source_address, amount
            FROM points_economy_events
            WHERE status='confirmed'
              AND chain_branch=?
              AND transaction_type IN ('margin_fee_retained', 'margin_interest_retained', 'margin_loss_collected')
            ORDER BY id ASC
            """,
            (branch,),
        ).fetchall()
        for row in rows:
            address = str(row["source_address"] or "").strip().lower()
            if not is_pc0_internal_address(address):
                continue
            amount = int(row["amount"] or 0)
            transaction_type = str(row["transaction_type"] or "")
            if transaction_type in {"margin_fee_retained", "margin_interest_retained"}:
                retained_by_address[address] = int(retained_by_address.get(address) or 0) + amount
            elif transaction_type == "margin_loss_collected":
                loss_collected_by_address[address] = int(loss_collected_by_address.get(address) or 0) + amount

        loss_debit_by_address = {}
        if table_columns(conn, "points_ledger"):
            rows = conn.execute(
                """
                SELECT user_id, COALESCE(SUM(amount), 0) AS amount
                FROM points_ledger
                WHERE status='confirmed'
                  AND direction='debit'
                  AND action_type='trading_margin_loss'
                GROUP BY user_id
                """
            ).fetchall()
            for row in rows:
                try:
                    user_id = int(row["user_id"] or 0)
                except (TypeError, ValueError):
                    user_id = 0
                address = address_by_user.get(user_id, "")
                if address:
                    loss_debit_by_address[address] = int(row["amount"] or 0)

        samples = []
        for address, raw_retained in sorted(retained_by_address.items()):
            retained = max(0, int(raw_retained or 0))
            if retained <= 0:
                continue
            loss_debit = max(0, int(loss_debit_by_address.get(address) or 0))
            loss_collected = max(0, int(loss_collected_by_address.get(address) or 0))
            covered = min(retained, max(0, loss_debit - loss_collected))
            unbacked = max(0, retained - covered)
            totals["covered_by_wallet_loss_debit_points"] += covered
            if unbacked <= 0:
                continue
            username = identity_by_address.get(address, "")
            if username == "root":
                bucket = "pc0_root_points"
            elif username:
                bucket = "pc0_member_points"
            else:
                bucket = "pc0_unknown_points"
            totals[bucket] += unbacked
            totals["total_points"] += unbacked
            totals["address_count"] += 1
            if len(samples) < 12:
                samples.append({
                    "address": address,
                    "points": unbacked,
                    "bucket": bucket,
                    "retained_points": retained,
                    "covered_by_wallet_loss_debit_points": covered,
                })
        totals["sample_addresses"] = samples
        return totals

    def _economy_external_balance_breakdown_locked(self, conn, *, external_balances, branch_uuid=None):
        external_balances = external_balances if isinstance(external_balances, dict) else {}
        identity_by_address = {}
        if table_columns(conn, "points_wallet_identities"):
            rows = conn.execute(
                """
                SELECT LOWER(w.address) AS address,
                       COALESCE(w.wallet_type, '') AS wallet_type,
                       COALESCE(w.custody_mode, '') AS custody_mode,
                       COALESCE(w.status, '') AS status,
                       COALESCE(LOWER(u.username), '') AS username
                FROM points_wallet_identities w
                LEFT JOIN users u ON u.id=w.user_id
                ORDER BY CASE WHEN w.status='active' THEN 0 ELSE 1 END, w.id ASC
                """
            ).fetchall()
            for row in rows:
                address = str(row["address"] or "").strip().lower()
                if address and address not in identity_by_address:
                    identity_by_address[address] = {
                        "wallet_type": str(row["wallet_type"] or ""),
                        "custody_mode": str(row["custody_mode"] or ""),
                        "status": str(row["status"] or ""),
                        "username": str(row["username"] or ""),
                    }

        breakdown = {
            "pc0_member_points": 0,
            "pc0_root_points": 0,
            "pc0_unknown_points": 0,
            "pc1_bound_cold_points": 0,
            "pc1_unbound_points": 0,
            "exchange_principal_receivable_points": 0,
            "malformed_pc0_points": 0,
            "other_unclassified_points": 0,
            "pc0_adjusted_by_exchange_principal_receivable_points": 0,
            "total_points": 0,
            "nonzero_address_count": 0,
            "sample_addresses": [],
        }
        samples = []
        for raw_address, raw_amount in sorted(external_balances.items()):
            try:
                amount = int(raw_amount or 0)
            except (TypeError, ValueError):
                amount = 0
            if amount == 0:
                continue
            address = str(raw_address or "").strip().lower()
            identity = identity_by_address.get(address)
            if is_pc0_internal_address(address):
                if identity and identity.get("username") == "root":
                    bucket = "pc0_root_points"
                elif identity:
                    bucket = "pc0_member_points"
                else:
                    bucket = "pc0_unknown_points"
            elif address.startswith("pc0"):
                bucket = "malformed_pc0_points"
            elif address.startswith("pc1"):
                bucket = "pc1_bound_cold_points" if identity else "pc1_unbound_points"
            elif address.startswith(EXCHANGE_PRINCIPAL_RECEIVABLE_ADDRESS_PREFIX):
                bucket = "exchange_principal_receivable_points"
            else:
                bucket = "other_unclassified_points"
            breakdown[bucket] += amount
            breakdown["total_points"] += amount
            breakdown["nonzero_address_count"] += 1
            if len(samples) < 12:
                samples.append({
                    "address": address,
                    "points": amount,
                    "bucket": bucket,
                    "wallet_type": identity.get("wallet_type", "") if identity else "",
                    "status": identity.get("status", "") if identity else "",
                })
        principal = self._exchange_principal_receivable_breakdown_locked(conn, branch_uuid=branch_uuid)
        for key in ("pc0_member_points", "pc0_root_points", "pc0_unknown_points"):
            adjustment = max(0, int(principal.get(key) or 0))
            if adjustment:
                applied = min(int(breakdown.get(key) or 0), adjustment)
                breakdown[key] -= applied
                breakdown["exchange_principal_receivable_points"] += applied
                breakdown["pc0_adjusted_by_exchange_principal_receivable_points"] += applied
        breakdown["pc0_internal_points"] = (
            breakdown["pc0_member_points"]
            + breakdown["pc0_root_points"]
            + breakdown["pc0_unknown_points"]
        )
        breakdown["cold_chain_or_bridge_external_points"] = (
            breakdown["pc1_bound_cold_points"]
            + breakdown["pc1_unbound_points"]
            + breakdown["exchange_principal_receivable_points"]
            + breakdown["malformed_pc0_points"]
            + breakdown["other_unclassified_points"]
        )
        breakdown["exchange_principal_receivable_breakdown"] = principal
        breakdown["sample_addresses"] = samples
        return breakdown

    def _pc0_bridge_flow_totals_locked(self, conn, *, branch_uuid=None):
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        totals = {
            "hot_to_cold_requested_points": 0,
            "hot_to_cold_confirmed_points": 0,
            "hot_to_cold_pending_points": 0,
            "hot_to_cold_failed_points": 0,
            "hot_to_cold_network_fee_points": 0,
            "invalid_direct_cold_to_pc0_request_points": 0,
            "deposit_credited_points": 0,
            "deposit_pending_points": 0,
            "deposit_failed_points": 0,
            "deposit_network_fee_points": 0,
            "economy_hot_to_external_points": 0,
            "economy_external_to_hot_points": 0,
            "economy_fund_to_external_points": 0,
            "economy_external_to_fund_points": 0,
            "economy_external_address_in_points": 0,
            "economy_external_address_out_points": 0,
            "economy_external_flow_net_points": 0,
            "pc0_request_net_outflow_points": 0,
        }
        if table_columns(conn, "points_chain_transfer_requests"):
            for row in conn.execute(
                """
                SELECT source_wallet_address, destination_wallet_address, amount_points,
                       fee_points, network_fee_points, status
                FROM points_chain_transfer_requests
                WHERE chain_branch=?
                """,
                (branch,),
            ).fetchall():
                source = str(row["source_wallet_address"] or "").strip().lower()
                destination = str(row["destination_wallet_address"] or "").strip().lower()
                amount = int(row["amount_points"] or 0)
                fee = int(row["network_fee_points"] if "network_fee_points" in row.keys() else row["fee_points"] or 0)
                status = str(row["status"] or "").strip().lower()
                source_pc0 = is_pc0_internal_address(source)
                destination_pc0 = is_pc0_internal_address(destination)
                if source_pc0 and not destination_pc0:
                    totals["hot_to_cold_requested_points"] += amount
                    if status == "confirmed":
                        totals["hot_to_cold_confirmed_points"] += amount
                        totals["hot_to_cold_network_fee_points"] += fee
                    elif status == "pending":
                        totals["hot_to_cold_pending_points"] += amount
                    elif status:
                        totals["hot_to_cold_failed_points"] += amount
                elif destination_pc0 and not source_pc0:
                    totals["invalid_direct_cold_to_pc0_request_points"] += amount

        if table_columns(conn, "points_chain_bridge_events"):
            for row in conn.execute(
                """
                SELECT bridge_type, status, amount_points, network_fee_points
                FROM points_chain_bridge_events
                WHERE bridge_type='deposit'
                """,
            ).fetchall():
                amount = int(row["amount_points"] or 0)
                fee = int(row["network_fee_points"] or 0)
                status = str(row["status"] or "").strip().lower()
                if status == "credited":
                    totals["deposit_credited_points"] += amount
                elif status == "pending":
                    totals["deposit_pending_points"] += amount
                elif status:
                    totals["deposit_failed_points"] += amount
                totals["deposit_network_fee_points"] += fee

        if table_columns(conn, "points_economy_events"):
            rows = conn.execute(
                """
                SELECT source_fund_key, source_address, destination_fund_key, destination_address, amount
                FROM points_economy_events
                WHERE status='confirmed' AND chain_branch=?
                ORDER BY id ASC
                """,
                (branch,),
            ).fetchall()
            for row in rows:
                amount = int(row["amount"] or 0)
                source_fund = str(row["source_fund_key"] or "").strip()
                destination_fund = str(row["destination_fund_key"] or "").strip()
                source = str(row["source_address"] or "").strip().lower()
                destination = str(row["destination_address"] or "").strip().lower()
                source_pc0 = is_pc0_internal_address(source)
                destination_pc0 = is_pc0_internal_address(destination)
                source_external = bool(source) and not source_pc0 and not self._explorer_fund_key_for_address(source)
                destination_external = bool(destination) and not destination_pc0 and not self._explorer_fund_key_for_address(destination)
                if destination_external:
                    totals["economy_external_address_in_points"] += amount
                if source_external:
                    totals["economy_external_address_out_points"] += amount
                if source_pc0 and destination_external:
                    totals["economy_hot_to_external_points"] += amount
                if source_external and destination_pc0:
                    totals["economy_external_to_hot_points"] += amount
                if source_fund and destination_external:
                    totals["economy_fund_to_external_points"] += amount
                if source_external and destination_fund:
                    totals["economy_external_to_fund_points"] += amount
        totals["economy_external_flow_net_points"] = (
            totals["economy_external_address_in_points"]
            - totals["economy_external_address_out_points"]
        )
        totals["pc0_request_net_outflow_points"] = (
            totals["hot_to_cold_confirmed_points"] - totals["deposit_credited_points"]
        )
        return totals

    def _official_hot_wallet_circulation_with_economy_breakdown_locked(self, conn, *, branch_uuid=None, actor=None):
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        circulation = self._official_hot_wallet_circulation_totals(conn, branch_uuid=branch)
        bootstrap_economy_layer(
            conn,
            chain_secret=self.chain_secret,
            actor=actor or {"role": "system", "id": None},
            chain_branch=branch,
        )
        replay = replay_economy_events(
            conn,
            chain_secret=self.chain_secret,
            persist_cache=False,
            chain_branch=branch,
        )
        breakdown = self._economy_external_balance_breakdown_locked(
            conn,
            external_balances=replay.get("external_balances") or {},
            branch_uuid=branch,
        )
        member_external = int(breakdown.get("pc0_member_points") or 0)
        root_external = int(breakdown.get("pc0_root_points") or 0)
        total_internal_external = member_external + root_external
        member_ledger = int(circulation.get("member_outstanding_points") or 0)
        root_ledger = int(circulation.get("root_outstanding_points") or 0)
        withheld = self._margin_settlement_withheld_breakdown_locked(conn, branch_uuid=branch)
        withheld_member = int(withheld.get("pc0_member_points") or 0)
        withheld_root = int(withheld.get("pc0_root_points") or 0)
        withheld_unknown = int(withheld.get("pc0_unknown_points") or 0)
        withheld_total = withheld_member + withheld_root + withheld_unknown
        flow_totals = self._pc0_bridge_flow_totals_locked(conn, branch_uuid=branch)
        flow_totals["current_cold_chain_or_bridge_external_points"] = int(breakdown.get("cold_chain_or_bridge_external_points") or 0)
        flow_totals["economy_flow_reconciliation_gap_points"] = (
            int(breakdown.get("cold_chain_or_bridge_external_points") or 0)
            - int(flow_totals.get("economy_external_flow_net_points") or 0)
        )
        circulation.update({
            "economy_external_balance_breakdown": breakdown,
            "bridge_flow_totals": flow_totals,
            "economy_pc0_member_internal_points": member_external,
            "economy_pc0_root_internal_points": root_external,
            "economy_pc0_unknown_internal_points": int(breakdown.get("pc0_unknown_points") or 0),
            "economy_pc0_internal_points": total_internal_external + int(breakdown.get("pc0_unknown_points") or 0),
            "margin_settlement_withheld_breakdown": withheld,
            "exchange_margin_settlement_withheld_contra_points": withheld_total,
            "economy_pc0_member_reconciled_points": member_external + withheld_member,
            "economy_pc0_root_reconciled_points": root_external + withheld_root,
            "economy_pc0_unknown_reconciled_points": int(breakdown.get("pc0_unknown_points") or 0) + withheld_unknown,
            "economy_pc0_reconciled_points": total_internal_external + int(breakdown.get("pc0_unknown_points") or 0) + withheld_total,
            "off_wallet_economy_external_points": int(breakdown.get("cold_chain_or_bridge_external_points") or 0),
            "ledger_vs_economy_external_gap_points": (member_ledger + root_ledger) - (total_internal_external + withheld_total),
            "member_ledger_vs_economy_external_gap_points": member_ledger - (member_external + withheld_member),
            "root_ledger_vs_economy_external_gap_points": root_ledger - (root_external + withheld_root),
        })
        return circulation

    def _economy_external_address_balance(self, conn, address, *, chain_branch=None):
        address = str(address or "").strip()
        if not address:
            return 0
        branch = str(chain_branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        rows = conn.execute(
            """
            SELECT source_fund_key, source_address, destination_fund_key, destination_address, amount
            FROM points_economy_events
            WHERE status='confirmed' AND chain_branch=?
            ORDER BY id ASC
            """
            ,
            (branch,),
        ).fetchall()
        balance = 0
        for row in rows:
            amount = int(row["amount"] or 0)
            if not row["destination_fund_key"] and str(row["destination_address"] or "") == address:
                balance += amount
            if not row["source_fund_key"] and str(row["source_address"] or "") == address:
                balance -= amount
        return balance

    def _append_walletized_economy_event(self, conn, *, ledger_row, public_metadata=None, actor=None):
        public_metadata = public_metadata if isinstance(public_metadata, dict) else {}
        if str(ledger_row["action_type"] or "") == "wallet_transfer_in":
            return None, False
        snapshot = public_metadata.get("wallet_flow_snapshot")
        flow = dict(snapshot) if isinstance(snapshot, dict) else self._legacy_ledger_wallet_flow_descriptor(
            conn,
            ledger_row,
            public_metadata=public_metadata,
        )
        if (
            not flow.get("walletized")
            and str(ledger_row["direction"] or "") in {"credit", "transfer_in"}
            and self._points_ledger_credit_source_fund(ledger_row["action_type"])
        ):
            flow = self._ledger_wallet_flow_descriptor(
                conn,
                user_id=int(ledger_row["user_id"]),
                direction=ledger_row["direction"],
                action_type=ledger_row["action_type"],
                public_metadata=public_metadata,
            )
            flow["walletization_note"] = "auto distribution source repaired from action_type for economy backfill"
        if flow.get("internal_movement"):
            return None, False
        if not flow.get("walletized"):
            return None, False
        source_fund_key = flow.get("source_fund_key")
        destination_fund_key = flow.get("destination_fund_key")
        source_address = "" if source_fund_key else flow.get("source_wallet_address")
        destination_address = "" if destination_fund_key else flow.get("destination_wallet_address")
        if not source_fund_key and not source_address:
            return None, False
        if not destination_fund_key and not destination_address:
            return None, False
        branch = str(ledger_row["chain_branch"] if "chain_branch" in ledger_row.keys() else self._canonical_branch_uuid(conn))
        bootstrap_economy_layer(conn, chain_secret=self.chain_secret, actor={"role": "system", "id": None}, chain_branch=branch)
        return append_economy_event(
            conn,
            chain_secret=self.chain_secret,
            event_type="legacy_walletized_flow",
            transaction_type=str(ledger_row["action_type"] or "points_ledger"),
            source_fund_key=source_fund_key,
            source_address=source_address,
            destination_fund_key=destination_fund_key,
            destination_address=destination_address,
            amount=int(ledger_row["amount"]),
            idempotency_key=f"walletized_ledger:{ledger_row['ledger_uuid']}",
            metadata={
                "legacy_ledger_uuid": ledger_row["ledger_uuid"],
                "legacy_action_type": ledger_row["action_type"],
                "legacy_direction": ledger_row["direction"],
                "legacy_user_id": int(ledger_row["user_id"]),
                "legacy_reference_type": ledger_row["reference_type"],
                "legacy_reference_id": ledger_row["reference_id"],
                "walletization_phase": "1B",
            },
            actor=actor,
            chain_branch=branch,
        )

    def _wallet_identity_owner_for_address(self, conn, address):
        address = str(address or "").strip().lower()
        if not WALLET_ADDRESS_RE.fullmatch(address):
            raise ValueError("wallet address format is invalid")
        return conn.execute(
            """
            SELECT * FROM points_wallet_identities
            WHERE address=? AND status IN ('pending_backup', 'active')
              AND (wallet_type='official_hot' OR custody_mode IN ('server_hot', 'system', 'multisig', 'self_custody'))
            LIMIT 1
            """,
            (address,),
        ).fetchone()

    def _assert_wallet_identity_can_spend(self, wallet, *, context="wallet transaction"):
        if not wallet:
            raise PermissionError("source wallet does not exist")
        keys = set(wallet.keys())
        custody_mode = str(wallet["custody_mode"] or "")
        wallet_scope = str(wallet["wallet_scope"] if "wallet_scope" in keys else "user")
        spend_capability = str(wallet["spend_capability"] if "spend_capability" in keys else "enabled")
        wallet_type = str(wallet["wallet_type"] or "")
        if custody_mode == "multisig" and wallet_scope != "official_treasury":
            raise PermissionError("user_multisig_receive_only_rc1: 一般用戶多簽目前僅支援收款/觀察，不支援轉出")
        if custody_mode == "multisig" and wallet_scope == "official_treasury" and wallet_type != "official_treasury_multisig":
            raise PermissionError("official treasury multisig spend requires official_treasury_multisig wallet type")
        if spend_capability != "enabled":
            raise PermissionError(f"{context} source wallet is {spend_capability}")

    def _transfer_request_payload_hash(self, payload):
        return sha256_text(canonical_json(payload))

    def _transfer_request_ledgers(self, conn, request_uuid):
        req = conn.execute(
            "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
            (request_uuid,),
        ).fetchone()
        if not req:
            return None
        ledgers = {}
        for key, column in (
            ("transfer_out_ledger", "transfer_out_ledger_uuid"),
            ("transfer_in_ledger", "transfer_in_ledger_uuid"),
            ("fee_ledger", "fee_ledger_uuid"),
        ):
            ledger_uuid = req[column]
            if ledger_uuid:
                row = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (ledger_uuid,)).fetchone()
                ledgers[key] = self._explorer_public_ledger(conn, row) if row else None
            else:
                ledgers[key] = None
        return {"request": dict(req), **ledgers}

    def _explorer_find_transfer_request(self, conn, ref):
        ref = str(ref or "").strip()
        if not ref:
            return None
        return conn.execute(
            """
            SELECT *
            FROM points_chain_transfer_requests
            WHERE request_uuid=?
               OR tx_group_hash=?
               OR transfer_out_ledger_uuid=?
               OR transfer_in_ledger_uuid=?
               OR fee_ledger_uuid=?
            LIMIT 1
            """,
            (ref, ref, ref, ref, ref),
        ).fetchone()

    def _pending_transfer_outgoing_for_address(self, conn, address, *, exclude_request_uuid=None, branch_uuid=None):
        address = str(address or "").strip().lower()
        if not address:
            return 0
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        sql = """
            SELECT COALESCE(SUM(amount_points + fee_points), 0) AS pending_total
            FROM points_chain_transfer_requests
            WHERE source_wallet_address=? AND chain_branch=? AND status='pending'
        """
        params = [address, branch]
        if exclude_request_uuid:
            sql += " AND request_uuid<>?"
            params.append(str(exclude_request_uuid))
        row = conn.execute(sql, tuple(params)).fetchone()
        return int((row["pending_total"] if row else 0) or 0)

    def _wallet_identity_balance_for_address(self, conn, *, user_id, address):
        state = self._wallet_identity_balances_for_user(conn, int(user_id), include_pending=False)
        balances = state.get("balances") or {}
        payload = balances.get(str(address or "").strip().lower())
        return int((payload or {}).get("balance") or 0)

    def _wallet_identity_available_for_address(self, conn, *, user_id, address, exclude_request_uuid=None):
        state = self._wallet_identity_balances_for_user(
            conn,
            int(user_id),
            include_pending=True,
            exclude_request_uuid=exclude_request_uuid,
        )
        balances = state.get("balances") or {}
        payload = balances.get(str(address or "").strip().lower())
        return int((payload or {}).get("balance") or 0)

    def _transfer_request_chain_fee_policy(self, req):
        req = dict(req)
        settlement_rail = str(req.get("settlement_rail") or "cold_chain")
        chain_required = bool(int(req.get("chain_required") if req.get("chain_required") is not None else 1))
        transaction_type = str(req.get("transaction_type") or "wallet_transfer")
        source_fund_key = str(req.get("source_fund_key") or "")
        official_grant = transaction_type == "official_wallet_grant" or source_fund_key == "official_treasury"
        destination_fund_key = self._explorer_fund_key_for_address(req.get("destination_wallet_address") or "")
        source_pc0 = is_pc0_internal_address(req.get("source_wallet_address") or "")
        destination_pc0 = is_pc0_internal_address(req.get("destination_wallet_address") or "")
        pc0_internal_transfer = bool(
            source_pc0
            and (
                destination_pc0
                or destination_fund_key in {"exchange_fund", "promo_fund", "burn"}
            )
            and official_grant
        )
        if pc0_internal_transfer or not chain_required or settlement_rail in {"internal_hot_wallet", "internal_system_burn"}:
            return {
                "base_fee_exempt": True,
                "base_fee_destination_fund_key": "burn",
                "base_fee_destination_label": "BURN 銷毀錢包",
                "acceleration_allowed": False,
                "exemption_reason": EXPLORER_INTERNAL_HOT_WALLET_HUMAN_RULE,
                "manual_official_wallet_ops_are_auto": False,
            }
        return {
            "base_fee_exempt": False,
            "base_fee_destination_fund_key": "burn",
            "base_fee_destination_label": "BURN 銷毀錢包",
            "acceleration_allowed": bool(str(req.get("status") or "pending") == "pending"),
            "exemption_reason": "",
            "manual_official_wallet_ops_are_auto": False,
        }

    def _transfer_request_public_payload(self, conn, req, *, network_state=None):
        req = dict(req)
        fee = int(req.get("fee_points") or 0)
        settlement_rail = str(req.get("settlement_rail") or "cold_chain")
        chain_required = bool(int(req.get("chain_required") if req.get("chain_required") is not None else 1))
        approval_required = bool(int(req.get("approval_required") if req.get("approval_required") is not None else 1))
        network_fee_points = int(req.get("network_fee_points") if req.get("network_fee_points") is not None else fee)
        service_fee_points = int(req.get("service_fee_points") or 0)
        acceleration = self._explorer_acceleration_summary(conn, req["request_uuid"])
        acceleration_fee = int(acceleration.get("total_fee_points") or 0)
        transaction_type = str(req.get("transaction_type") or "wallet_transfer")
        source_fund_key = str(req.get("source_fund_key") or "")
        official_grant = transaction_type == "official_wallet_grant" or source_fund_key == "official_treasury"
        destination_fund_key = self._explorer_fund_key_for_address(req.get("destination_wallet_address") or "")
        destination_unowned = self._transfer_request_destination_unowned(req)
        official_fund_transfer = bool(official_grant and destination_fund_key)
        source_pc0 = is_pc0_internal_address(req.get("source_wallet_address") or "")
        destination_pc0 = is_pc0_internal_address(req.get("destination_wallet_address") or "")
        pc0_internal_transfer = bool(
            source_pc0
            and (
                destination_pc0
                or destination_fund_key in {"exchange_fund", "promo_fund", "burn"}
            )
            and official_grant
        )
        if pc0_internal_transfer:
            settlement_rail = "internal_system_burn" if destination_fund_key == "burn" else "internal_hot_wallet"
            chain_required = False
            approval_required = False
            network_fee_points = 0
            service_fee_points = 0
        if not chain_required or settlement_rail in {"internal_hot_wallet", "internal_system_burn"}:
            finality = self._explorer_internal_hot_wallet_finality()
        else:
            estimate = self._explorer_finality_estimate(
                fee + acceleration_fee,
                conn=conn,
                network_state=network_state,
            )
            pseudo_ledger = {
                "ledger_uuid": req["request_uuid"],
                "ledger_hash": req["tx_group_hash"],
                "created_at": req["created_at"],
            }
            schedule = self._explorer_finality_schedule(pseudo_ledger, estimate)
            finality = self._explorer_finality_from_created(
                created_at=req["created_at"],
                estimate=estimate,
                schedule=schedule,
                sealed=False,
                fee_policy=self._transfer_request_chain_fee_policy(req),
                acceleration_fee_points=fee + acceleration_fee,
            )
        finality.update({
            "base_transaction_fee_points": fee,
            "acceleration_request_count": int(acceleration.get("count") or 0),
            "acceleration_fee_paid_points": acceleration_fee,
            "acceleration_fee_destination_fund_key": "burn" if acceleration_fee else "",
            "acceleration_fee_destination_label": "BURN 銷毀錢包" if acceleration_fee else "",
            "latest_acceleration_request": acceleration.get("latest_request"),
        })
        status = str(req.get("status") or "pending")
        if status == "confirmed" and finality["finality_status"] == "pending":
            finality.update({
                "proved_count": EXPLORER_FINALITY_PROVED_COUNT,
                "proved_remaining": 0,
                "finality_status": "proved",
                "eta_seconds": 0,
                "next_proof_eta_seconds": 0,
            })
        elif status.startswith("failed"):
            finality.update({
                "finality_status": "failed",
                "block_status": "failed",
                "eta_seconds": 0,
                "next_proof_eta_seconds": 0,
            })
        out_row = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (req.get("transfer_out_ledger_uuid") or "",)).fetchone()
        in_row = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (req.get("transfer_in_ledger_uuid") or "",)).fetchone()
        fee_row = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (req.get("fee_ledger_uuid") or "",)).fetchone()
        block = None
        block_source = out_row or in_row or fee_row
        if block_source and block_source["chain_block_id"]:
            block_row = conn.execute("SELECT * FROM points_chain_blocks WHERE id=?", (block_source["chain_block_id"],)).fetchone()
            if block_row:
                block = {
                    "block_number": int(block_row["block_number"]),
                    "block_hash": block_row["block_hash"],
                    "sealed_at": block_row["sealed_at"],
                    "ledger_count": int(block_row["ledger_count"]),
                }
                finality["block_status"] = "sealed"
        input_data = {"memo": req.get("memo") or "", "request_uuid": req["request_uuid"]}
        input_data["transaction_type"] = transaction_type
        input_data["settlement_rail"] = settlement_rail
        source_label = "官方 Treasury 錢包" if official_grant else "From"
        destination_label = destination_fund_key.replace("_", " ").title() if destination_fund_key else "To"
        reason = (
            "official treasury fund transfer"
            if official_fund_transfer
            else "official treasury wallet grant"
            if official_grant
            else "wallet transfer"
        )
        layer = self._explorer_layer_for_rail(
            settlement_rail,
            source_address=req.get("source_wallet_address"),
            destination_address=req.get("destination_wallet_address"),
        )
        return {
            "layer": layer,
            "asset_type": self._explorer_asset_type_for_layer(layer, settlement_rail),
            "ledger_uuid": req["request_uuid"],
            "chain_branch": req.get("chain_branch") or self._main_branch_uuid(),
            "branch": self._branch_metadata(conn, req.get("chain_branch") or self._main_branch_uuid()),
            "ledger_hash": req["tx_group_hash"],
            "transaction_hash": req["tx_group_hash"],
            "previous_ledger_hash": "",
            "public_account_id": "",
            "currency_type": DISPLAY_CURRENCY,
            "direction": "transfer_out" if official_fund_transfer else ("credit" if official_grant else "transfer_out"),
            "amount": int(req.get("amount_points") or 0),
            "amount_points": int(req.get("amount_points") or 0),
            "fee_points": fee,
            "action_type": transaction_type,
            "reference_type": transaction_type,
            "reference_id": req["tx_group_hash"],
            "reason": reason,
            "input_data": input_data,
            "status": status,
            "created_at": req["created_at"],
            "chain_block_id": block_source["chain_block_id"] if block_source else None,
            "settlement_rail": settlement_rail,
            "chain_required": chain_required,
            "approval_required": approval_required,
            "network_fee_points": network_fee_points,
            "service_fee_points": service_fee_points,
            "wallet_flow": {
                "source_fund_key": source_fund_key or None,
                "destination_fund_key": destination_fund_key or None,
                "source_label": source_label,
                "source_wallet_address": req["source_wallet_address"],
                "destination_label": destination_label,
                "destination_wallet_address": req["destination_wallet_address"],
                "destination_unowned": destination_unowned,
                "destination_owner_known": not destination_unowned,
                "target_wallet_address": "",
                "walletized": True,
                "walletization_note": (
                    "pc0 internal settlement credits immediately without 20/20 Proved"
                    if not chain_required
                    else "pending requests do not credit the recipient until 20/20 Proved"
                ),
                "settlement_rail": settlement_rail,
                "chain_required": chain_required,
                "approval_required": approval_required,
                "network_fee_points": network_fee_points,
                "service_fee_points": service_fee_points,
            },
            "block": block,
            "finality": finality,
            "transfer_ledgers": {
                "transfer_out_ledger_uuid": req.get("transfer_out_ledger_uuid") or "",
                "transfer_in_ledger_uuid": req.get("transfer_in_ledger_uuid") or "",
                "fee_ledger_uuid": req.get("fee_ledger_uuid") or "",
            },
            "cross_references": {
                "bridge_event_uuid": "",
                "pc1_settlement_tx": req["tx_group_hash"] if layer == "pc1" else "",
                "pc0_wrapped_credit": req.get("transfer_in_ledger_uuid") or "",
            },
        }

    def _transfer_request_member_summary(self, conn, req, *, user_id, root_view=False, viewer_addresses=None):
        req = dict(req)
        payload = self._transfer_request_public_payload(conn, req)
        sender_id = int(req["sender_user_id"] or 0)
        recipient_id = self._transfer_request_recipient_user_id(req)
        viewer_id = int(user_id)
        viewer_addresses = {str(item or "").strip().lower() for item in (viewer_addresses or []) if item}
        source_address = str(req["source_wallet_address"] or "").strip().lower()
        destination_address = str(req["destination_wallet_address"] or "").strip().lower()
        destination_unowned = self._transfer_request_destination_unowned(req)
        source_is_viewer = sender_id == viewer_id or source_address in viewer_addresses
        destination_is_viewer = destination_address in viewer_addresses or (recipient_id == viewer_id and not destination_unowned)
        transaction_type = str(req.get("transaction_type") or "wallet_transfer")
        settlement_rail = str(payload.get("settlement_rail") or req.get("settlement_rail") or "cold_chain")
        source_fund_key = str(req.get("source_fund_key") or "")
        destination_fund_key = self._explorer_fund_key_for_address(req.get("destination_wallet_address") or "")
        if root_view and source_fund_key == "official_treasury" and destination_fund_key:
            direction = "official_fund_transfer"
            counterparty = req["destination_wallet_address"]
        elif root_view and (transaction_type == "official_wallet_grant" or source_fund_key == "official_treasury"):
            direction = "official_outgoing"
            counterparty = req["destination_wallet_address"]
        elif root_view:
            direction = "observed"
            counterparty = req["destination_wallet_address"]
        elif source_is_viewer and destination_is_viewer:
            direction = "self"
            counterparty = req["destination_wallet_address"]
        elif source_is_viewer:
            direction = "outgoing"
            counterparty = req["destination_wallet_address"]
        else:
            direction = "incoming"
            counterparty = req["source_wallet_address"]
        status = str(req["status"] or "pending")
        return {
            "request_uuid": req["request_uuid"],
            "tx_group_hash": req["tx_group_hash"],
            "transaction_hash": req["tx_group_hash"],
            "transaction_type": transaction_type,
            "settlement_rail": settlement_rail,
            "chain_required": _metadata_bool(payload.get("chain_required"), default=True),
            "approval_required": bool(payload.get("approval_required")),
            "network_fee_points": int(payload.get("network_fee_points") or 0),
            "service_fee_points": int(payload.get("service_fee_points") or 0),
            "source_fund_key": source_fund_key,
            "direction": direction,
            "status": status,
            "amount_points": int(req["amount_points"] or 0),
            "fee_points": int(req["fee_points"] or 0),
            "source_wallet_address": req["source_wallet_address"],
            "destination_wallet_address": req["destination_wallet_address"],
            "counterparty_wallet_address": counterparty,
            "memo": req["memo"] or "",
            "created_at": req["created_at"],
            "finality": payload["finality"],
            "wallet_flow": payload["wallet_flow"],
            "transfer_ledgers": payload["transfer_ledgers"],
            "balance_effect": (
                "confirmed"
                if status == "confirmed"
                else "pending_no_recipient_credit"
                if status == "pending"
                else "failed_no_balance_change"
            ),
        }

    def _notify_wallet_transfer_once(self, conn, *, user_id, notification_type, title, body, tx_group_hash):
        try:
            from services.system.notifications import create_notification_once_if_enabled
            create_notification_once_if_enabled(
                conn,
                user_id=int(user_id),
                type=notification_type,
                title=title,
                body=body,
                link=f"/#economy-explorer:{tx_group_hash}",
            )
        except Exception as exc:
            try:
                self._audit_log(
                    conn,
                    "POINTS_CHAIN_NOTIFICATION_FAILED",
                    "warning",
                    f"{notification_type} notification failed for user {user_id}",
                    target_user_id=int(user_id),
                    metadata={
                        "notification_type": notification_type,
                        "transaction_hash": tx_group_hash,
                        "error": str(exc)[:240],
                    },
                )
            except Exception:
                pass
            return False
        return True

    def _credit_confirmed_deposit_transfer_locked(self, conn, req, *, deposit_row, actor=None):
        req = dict(req)
        deposit_user_id = int(deposit_row["user_id"] or 0)
        if deposit_user_id <= 0:
            raise ValueError("deposit address is not assigned to a valid user")
        chain = str(deposit_row["chain"] or "points_chain_sim").strip() or "points_chain_sim"
        tx_hash = str(req["tx_group_hash"] or "").strip().lower()
        source = str(req["source_wallet_address"] or "").strip().lower()
        destination = str(req["destination_wallet_address"] or "").strip().lower()
        deposit_address = str(deposit_row["address"] or "").strip().lower()
        amount = int(req["amount_points"] or 0)
        if amount <= 0:
            raise ValueError("deposit transfer amount must be positive")
        if destination != deposit_address:
            raise ValueError("deposit transfer destination mismatch")
        network_fee = int(req["network_fee_points"] if "network_fee_points" in req.keys() and req["network_fee_points"] is not None else req["fee_points"] or 0)
        hot_wallet = create_official_hot_wallet(conn, user_id=deposit_user_id, chain_secret=self.chain_secret)
        existing = conn.execute(
            """
            SELECT *
            FROM points_chain_bridge_events
            WHERE chain=? AND chain_tx_hash=?
              AND chain_tx_hash<>''
            LIMIT 1
            """,
            (chain, tx_hash),
        ).fetchone()
        if existing:
            if str(existing["bridge_type"] or "deposit") != "deposit":
                raise ValueError("deposit chain_tx_hash belongs to a non-deposit bridge event")
            if int(existing["user_id"] or 0) != deposit_user_id:
                raise ValueError("deposit chain_tx_hash is already assigned to another user")
            if int(existing["amount_points"] or 0) != amount:
                raise ValueError("deposit chain_tx_hash idempotency conflict")
            if str(existing["source_address"] or "").strip().lower() != source:
                raise ValueError("deposit chain_tx_hash source mismatch")
            if str(existing["destination_address"] or "").strip().lower() != destination:
                raise ValueError("deposit chain_tx_hash destination mismatch")
            status = str(existing["status"] or "pending").strip().lower()
            risk_status = str(existing["risk_status"] or "accepted").strip().lower()
            ledger = None
            if existing["internal_ledger_uuid"]:
                ledger = conn.execute(
                    "SELECT * FROM points_ledger WHERE ledger_uuid=?",
                    (existing["internal_ledger_uuid"],),
                ).fetchone()
            if status == "credited" or ledger:
                self._sync_confirmed_deposit_bridge_to_economy_locked(
                    conn,
                    bridge_row=existing,
                    req=req,
                    actor=actor,
                )
                return existing, ledger, False
            if status in {"failed", "refunded"} or risk_status != "accepted":
                return existing, None, False
        bridge, ledger, created = self._deposit_bridge_credit_locked(
            conn,
            actor=actor,
            bridge_row=existing,
            user_id=deposit_user_id,
            amount=amount,
            chain=chain,
            tx_hash=tx_hash,
            source=source,
            destination=destination,
            hot_wallet_address=hot_wallet["address"],
            deposit_address=deposit_address,
            confirmations=EXPLORER_FINALITY_PROVED_COUNT,
            required_confirmations=EXPLORER_FINALITY_PROVED_COUNT,
            network_fee_points=network_fee,
            metadata={
                "auto_detected_from_transfer_request": True,
                "request_uuid": req["request_uuid"],
                "transfer_settlement_rail": req["settlement_rail"] if "settlement_rail" in req.keys() else "",
                "transfer_fee_points": int(req["fee_points"] or 0),
            },
        )
        self._sync_confirmed_deposit_bridge_to_economy_locked(
            conn,
            bridge_row=bridge,
            req=req,
            actor=actor,
        )
        return bridge, ledger, created

    def _sync_confirmed_deposit_bridge_to_economy_locked(self, conn, *, bridge_row, req=None, actor=None):
        if not bridge_row or str(bridge_row["status"] or "").strip().lower() != "credited":
            return None, False
        source_address = str(bridge_row["destination_address"] or "").strip().lower()
        destination_address = str(bridge_row["hot_wallet_address"] or "").strip().lower()
        if not source_address or not destination_address:
            return None, False
        amount = int(bridge_row["amount_points"] or 0)
        if amount <= 0:
            return None, False
        branch = self._canonical_branch_uuid(conn)
        if req is not None:
            try:
                branch = str(dict(req).get("chain_branch") or branch)
            except Exception:
                pass
        bootstrap_economy_layer(conn, chain_secret=self.chain_secret, actor={"role": "system", "id": None}, chain_branch=branch)
        return append_economy_event(
            conn,
            chain_secret=self.chain_secret,
            event_type="deposit_bridge_credit",
            transaction_type="deposit_bridge_credit",
            source_fund_key=None,
            source_address=source_address,
            destination_fund_key=None,
            destination_address=destination_address,
            amount=amount,
            idempotency_key=f"deposit_bridge_credit_economy:{bridge_row['bridge_uuid']}",
            metadata={
                "bridge_uuid": bridge_row["bridge_uuid"],
                "chain_tx_hash": bridge_row["chain_tx_hash"],
                "source_chain_address": bridge_row["source_address"],
                "deposit_address": bridge_row["destination_address"],
                "hot_wallet_address": bridge_row["hot_wallet_address"],
                "network_fee_points": int(bridge_row["network_fee_points"] or 0),
                "request_uuid": str(dict(req).get("request_uuid") or "") if req is not None else "",
                "walletization_phase": "pc0_bridge_v1",
                "financial_source_of_truth": "points_chain_bridge_events",
            },
            actor=actor,
            chain_branch=branch,
        )

    def _reconcile_confirmed_deposit_transfers_locked(self, conn, *, actor=None, limit=1000):
        if not table_columns(conn, "points_chain_transfer_requests") or not table_columns(conn, "points_chain_deposit_addresses"):
            return {"checked_count": 0, "credited_count": 0, "linked_count": 0, "skipped_count": 0}
        limit = min(5000, max(1, int(limit or 1000)))
        rows = conn.execute(
            """
            SELECT r.*
            FROM points_chain_transfer_requests r
            JOIN points_chain_deposit_addresses d
              ON LOWER(r.destination_wallet_address)=LOWER(d.address)
             AND d.status IN ('active', 'rotated')
            WHERE r.status='confirmed'
            ORDER BY r.id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        checked = 0
        credited = 0
        linked = 0
        skipped = 0
        for row in rows:
            checked += 1
            deposit_row = self._active_deposit_address_for_address(
                conn,
                row["destination_wallet_address"],
                include_rotated=True,
            )
            if not deposit_row:
                skipped += 1
                continue
            bridge, ledger, ledger_created = self._credit_confirmed_deposit_transfer_locked(
                conn,
                row,
                deposit_row=deposit_row,
                actor=actor,
            )
            if ledger:
                updates = []
                params = []
                if int(row["recipient_user_id"] or 0) != int(deposit_row["user_id"] or 0):
                    updates.append("recipient_user_id=?")
                    params.append(int(deposit_row["user_id"] or 0))
                if self._transfer_request_destination_unowned(row):
                    updates.append("destination_unowned=0")
                if str(row["transfer_in_ledger_uuid"] or "") != str(ledger["ledger_uuid"] or ""):
                    updates.append("transfer_in_ledger_uuid=?")
                    params.append(ledger["ledger_uuid"])
                if updates:
                    params.append(row["request_uuid"])
                    conn.execute(
                        f"UPDATE points_chain_transfer_requests SET {', '.join(updates)} WHERE request_uuid=?",
                        tuple(params),
                    )
                    linked += 1
                if ledger_created:
                    credited += 1
            elif bridge:
                skipped += 1
        return {
            "checked_count": checked,
            "credited_count": credited,
            "linked_count": linked,
            "skipped_count": skipped,
        }

    def _transfer_request_is_official_grant(self, req):
        req = dict(req)
        return (
            str(req.get("transaction_type") or "wallet_transfer") == "official_wallet_grant"
            or str(req.get("source_fund_key") or "") == "official_treasury"
        )

    def _transfer_request_uses_internal_settlement(self, req):
        req = dict(req)
        settlement_rail = str(req.get("settlement_rail") or "").strip()
        if settlement_rail in {"internal_hot_wallet", "internal_system_burn"}:
            return True
        if str(req.get("source_fund_key") or "") != "official_treasury":
            return False
        source_pc0 = is_pc0_internal_address(req.get("source_wallet_address") or "")
        destination = str(req.get("destination_wallet_address") or "").strip().lower()
        return bool(source_pc0 and (is_pc0_internal_address(destination) or self._explorer_fund_key_for_address(destination)))

    def _transfer_request_destination_unowned(self, req):
        try:
            return bool(int(dict(req).get("destination_unowned") or 0))
        except Exception:
            return False

    def _transfer_request_recipient_user_id(self, req):
        try:
            return int(dict(req).get("recipient_user_id") or 0)
        except Exception:
            return 0

    def _notify_wallet_transfer_pending(self, conn, req):
        req = dict(req)
        amount = int(req["amount_points"] or 0)
        fee = int(req["fee_points"] or 0)
        tx_hash = req["tx_group_hash"]
        destination_unowned = self._transfer_request_destination_unowned(req)
        if self._transfer_request_is_official_grant(req):
            destination_fund_key = self._explorer_fund_key_for_address(req.get("destination_wallet_address") or "")
            if destination_fund_key:
                sender_sent = self._notify_wallet_transfer_once(
                    conn,
                    user_id=req["sender_user_id"],
                    notification_type="points_chain_official_fund_transfer_pending",
                    title="官方基金調撥交易已送出",
                    body=f"交易 {tx_hash} 正在等待 20/20 Proved；官方 Treasury 將調撥 {amount} 點到 {destination_fund_key}。成交前基金不會入帳。",
                    tx_group_hash=tx_hash,
                )
                return {
                    "event": "pending",
                    "sender": sender_sent,
                    "recipient": True,
                    "system_recipient": destination_fund_key,
                    "all_sent": bool(sender_sent),
                }
            sender_sent = self._notify_wallet_transfer_once(
                conn,
                user_id=req["sender_user_id"],
                notification_type="points_chain_official_grant_pending",
                title="官方發點交易已送出",
                body=f"交易 {tx_hash} 正在等待 20/20 Proved；官方 Treasury 發點 {amount} 點。成交前目的地址不會入帳。",
                tx_group_hash=tx_hash,
            )
            if destination_unowned:
                return {
                    "event": "pending",
                    "sender": sender_sent,
                    "recipient": None,
                    "unowned_recipient": True,
                    "all_sent": bool(sender_sent),
                }
            recipient_sent = self._notify_wallet_transfer_once(
                conn,
                user_id=req["recipient_user_id"],
                notification_type="points_chain_official_grant_pending",
                title="收到待確認官方發點",
                body=f"交易 {tx_hash} 正在等待 20/20 Proved；Value {amount} 點。成交前不會入帳。",
                tx_group_hash=tx_hash,
            )
            return {"event": "pending", "sender": sender_sent, "recipient": recipient_sent, "all_sent": bool(sender_sent and recipient_sent)}
        sender_sent = self._notify_wallet_transfer_once(
            conn,
            user_id=req["sender_user_id"],
            notification_type="points_chain_transfer_pending",
            title="鏈上交易已送出",
            body=f"交易 {tx_hash} 正在等待 20/20 Proved；Value {amount} 點，Fee {fee} 點。成交前收款方不會入帳。",
            tx_group_hash=tx_hash,
        )
        if destination_unowned:
            return {
                "event": "pending",
                "sender": sender_sent,
                "recipient": None,
                "unowned_recipient": True,
                "all_sent": bool(sender_sent),
            }
        recipient_sent = self._notify_wallet_transfer_once(
            conn,
            user_id=req["recipient_user_id"],
            notification_type="points_chain_transfer_pending",
            title="收到待確認鏈上交易",
            body=f"交易 {tx_hash} 正在等待 20/20 Proved；Value {amount} 點。成交前不會入帳。",
            tx_group_hash=tx_hash,
        )
        return {"event": "pending", "sender": sender_sent, "recipient": recipient_sent, "all_sent": bool(sender_sent and recipient_sent)}

    def _notify_wallet_transfer_completed(self, conn, req):
        req = dict(req)
        amount = int(req["amount_points"] or 0)
        fee = int(req["fee_points"] or 0)
        tx_hash = req["tx_group_hash"]
        destination_unowned = self._transfer_request_destination_unowned(req)
        internal_settlement = self._transfer_request_uses_internal_settlement(req)
        if self._transfer_request_is_official_grant(req):
            destination_fund_key = self._explorer_fund_key_for_address(req.get("destination_wallet_address") or "")
            if destination_fund_key:
                sender_sent = self._notify_wallet_transfer_once(
                    conn,
                    user_id=req["sender_user_id"],
                    notification_type="points_chain_official_fund_transfer_completed",
                    title="官方基金調撥已完成",
                    body=(
                        f"交易 {tx_hash} 已由 pc0 站內帳本即時調撥；官方 Treasury 已調撥 {amount} 點到 {destination_fund_key}。"
                        if internal_settlement
                        else f"交易 {tx_hash} 已達 20/20 Proved；官方 Treasury 已調撥 {amount} 點到 {destination_fund_key}。"
                    ),
                    tx_group_hash=tx_hash,
                )
                return {
                    "event": "completed",
                    "sender": sender_sent,
                    "recipient": True,
                    "system_recipient": destination_fund_key,
                    "all_sent": bool(sender_sent),
                }
            sender_sent = self._notify_wallet_transfer_once(
                conn,
                user_id=req["sender_user_id"],
                notification_type="points_chain_official_grant_completed",
                title="官方發點交易已成交",
                body=(
                    f"交易 {tx_hash} 已由 pc0 站內帳本即時完成；官方 Treasury 已發出 {amount} 點。"
                    if internal_settlement
                    else f"交易 {tx_hash} 已達 20/20 Proved；官方 Treasury 已發出 {amount} 點。"
                ),
                tx_group_hash=tx_hash,
            )
            if destination_unowned:
                return {
                    "event": "completed",
                    "sender": sender_sent,
                    "recipient": None,
                    "unowned_recipient": True,
                    "all_sent": bool(sender_sent),
                }
            recipient_sent = self._notify_wallet_transfer_once(
                conn,
                user_id=req["recipient_user_id"],
                notification_type="points_chain_official_grant_completed",
                title="官方發點已入帳",
                body=(
                    f"交易 {tx_hash} 已由 pc0 站內帳本即時完成；已入帳 {amount} 點。"
                    if internal_settlement
                    else f"交易 {tx_hash} 已達 20/20 Proved；已入帳 {amount} 點。"
                ),
                tx_group_hash=tx_hash,
            )
            return {"event": "completed", "sender": sender_sent, "recipient": recipient_sent, "all_sent": bool(sender_sent and recipient_sent)}
        sender_sent = self._notify_wallet_transfer_once(
            conn,
            user_id=req["sender_user_id"],
            notification_type="points_chain_transfer_completed",
            title="站內轉帳已完成" if internal_settlement else "鏈上交易已成交",
            body=(
                f"交易 {tx_hash} 已由 pc0 站內帳本即時完成；已扣除 Value {amount} 點，Fee 0 點。"
                if internal_settlement
                else f"交易 {tx_hash} 已達 20/20 Proved；已扣除 Value {amount} 點與 Fee {fee} 點。"
            ),
            tx_group_hash=tx_hash,
        )
        if destination_unowned:
            return {
                "event": "completed",
                "sender": sender_sent,
                "recipient": None,
                "unowned_recipient": True,
                "all_sent": bool(sender_sent),
            }
        deposit_destination = self._active_deposit_address_for_address(
            conn,
            req.get("destination_wallet_address") or "",
            include_rotated=True,
        )
        if deposit_destination:
            return {
                "event": "completed",
                "sender": sender_sent,
                "recipient": "deposit_bridge_credit",
                "all_sent": bool(sender_sent),
            }
        recipient_sent = self._notify_wallet_transfer_once(
            conn,
            user_id=req["recipient_user_id"],
            notification_type="points_chain_transfer_completed",
            title="站內轉帳已入帳" if internal_settlement else "鏈上交易已入帳",
            body=(
                f"交易 {tx_hash} 已由 pc0 站內帳本即時完成；已入帳 {amount} 點。"
                if internal_settlement
                else f"交易 {tx_hash} 已達 20/20 Proved；已入帳 {amount} 點。"
            ),
            tx_group_hash=tx_hash,
        )
        return {"event": "completed", "sender": sender_sent, "recipient": recipient_sent, "all_sent": bool(sender_sent and recipient_sent)}

    def _notify_wallet_transfer_failed(self, conn, req, reason):
        req = dict(req)
        tx_hash = req["tx_group_hash"]
        body = f"交易 {tx_hash} 未成交：{public_currency_text(reason or '交易失敗')}"
        sender_sent = self._notify_wallet_transfer_once(
            conn,
            user_id=req["sender_user_id"],
            notification_type="points_chain_transfer_failed",
            title="鏈上交易未成交",
            body=body,
            tx_group_hash=tx_hash,
        )
        if self._transfer_request_destination_unowned(req):
            return {
                "event": "failed",
                "sender": sender_sent,
                "recipient": None,
                "unowned_recipient": True,
                "all_sent": bool(sender_sent),
            }
        recipient_sent = self._notify_wallet_transfer_once(
            conn,
            user_id=req["recipient_user_id"],
            notification_type="points_chain_transfer_failed",
            title="鏈上交易未成交",
            body=body,
            tx_group_hash=tx_hash,
        )
        return {"event": "failed", "sender": sender_sent, "recipient": recipient_sent, "all_sent": bool(sender_sent and recipient_sent)}

    def _fail_transfer_request_locked(self, conn, req, *, status, reason):
        req = dict(req)
        status_text = str(status or "failed").strip() or "failed"
        if not status_text.startswith("failed"):
            status_text = f"failed_{status_text}"
        conn.execute(
            "UPDATE points_chain_transfer_requests SET status=? WHERE request_uuid=? AND status='pending'",
            (status_text[:80], req["request_uuid"]),
        )
        self._refresh_wallet_identity_pending_for_address_locked(
            conn,
            req.get("source_wallet_address"),
            branch=req.get("chain_branch") or self._canonical_branch_uuid(conn),
        )
        failed = conn.execute(
            "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
            (req["request_uuid"],),
        ).fetchone()
        self._notify_wallet_transfer_failed(conn, failed, reason)
        return failed

    def _maybe_finalize_transfer_request_locked(self, conn, req, *, actor=None):
        req = dict(req)
        if str(req.get("status") or "") != "pending":
            return conn.execute(
                "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
                (req["request_uuid"],),
            ).fetchone()
        req_branch = str(req.get("chain_branch") or self._main_branch_uuid())
        canonical_branch = self._canonical_branch_uuid(conn)
        if req_branch != canonical_branch:
            return self._fail_transfer_request_locked(
                conn,
                req,
                status="failed_non_canonical_branch",
                reason="transaction belongs to a non-canonical branch after governance recovery fork",
            )
        payload = self._transfer_request_public_payload(conn, req)
        if payload["finality"]["finality_status"] not in {"proved", "internal_settled"}:
            return conn.execute(
                "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
                (req["request_uuid"],),
            ).fetchone()
        if self._transfer_request_is_official_grant(req):
            destination_fund_key = self._explorer_fund_key_for_address(req["destination_wallet_address"])
            if destination_fund_key:
                try:
                    destination_fund_key = self._official_transfer_destination_fund_key(req["destination_wallet_address"])
                except ValueError as exc:
                    return self._fail_transfer_request_locked(
                        conn,
                        req,
                        status="failed_unsupported_system_fund",
                        reason=str(exc),
                    )
                bootstrap_economy_layer(conn, chain_secret=self.chain_secret, actor={"role": "system", "id": None}, chain_branch=req_branch)
                append_economy_event(
                    conn,
                    chain_secret=self.chain_secret,
                    event_type="official_fund_transfer",
                    transaction_type="official_fund_transfer",
                    source_fund_key="official_treasury",
                    destination_fund_key=destination_fund_key,
                    amount=int(req["amount_points"]),
                    idempotency_key=f"official_fund_transfer:{req['request_uuid']}",
                    metadata={
                        "request_uuid": req["request_uuid"],
                        "tx_group_hash": req["tx_group_hash"],
                        "source_wallet_address": req["source_wallet_address"],
                        "destination_wallet_address": req["destination_wallet_address"],
                        "destination_fund_key": destination_fund_key,
                        "memo": req["memo"] or "",
                        "walletization_phase": "1B",
                        "financial_source_of_truth": "points_economy_events",
                    },
                    actor=actor,
                    chain_branch=req_branch,
                )
                if destination_fund_key == "exchange_fund":
                    self._sync_exchange_fund_transfer_to_trading_reserve_pool(
                        conn,
                        req=req,
                        amount=int(req["amount_points"]),
                        actor=actor,
                    )
                conn.execute(
                    """
                    UPDATE points_chain_transfer_requests
                    SET status='confirmed'
                    WHERE request_uuid=? AND status='pending'
                    """,
                    (req["request_uuid"],),
                )
                self._refresh_wallet_identity_pending_for_address_locked(
                    conn,
                    req.get("source_wallet_address"),
                    branch=req_branch,
                )
                finalized = conn.execute(
                    "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
                    (req["request_uuid"],),
                ).fetchone()
                self._audit_log(
                    conn,
                    "OFFICIAL_FUND_TRANSFER",
                    "info",
                    f"official treasury transfer {int(req['amount_points'])} {DISPLAY_CURRENCY} to {destination_fund_key}",
                    actor=actor,
                    target_user_id=int(req["recipient_user_id"]),
                    metadata={
                        "destination_fund_key": destination_fund_key,
                        "destination_wallet_address": req["destination_wallet_address"],
                        "transaction_hash": req["tx_group_hash"],
                        "request_uuid": req["request_uuid"],
                        "finalized": True,
                    },
                )
                self._notify_wallet_transfer_completed(conn, finalized)
                return finalized
            destination_wallet = self._wallet_identity_owner_for_address(conn, req["destination_wallet_address"])
            if destination_wallet and destination_wallet["user_id"]:
                recipient_user_id = int(destination_wallet["user_id"])
                if recipient_user_id != self._transfer_request_recipient_user_id(req) or self._transfer_request_destination_unowned(req):
                    conn.execute(
                        """
                        UPDATE points_chain_transfer_requests
                        SET recipient_user_id=?, destination_unowned=0
                        WHERE request_uuid=?
                        """,
                        (recipient_user_id, req["request_uuid"]),
                    )
                    req = dict(conn.execute(
                        "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
                        (req["request_uuid"],),
                    ).fetchone())
            else:
                recipient_user_id = self._transfer_request_recipient_user_id(req)
            common = {
                "source_fund_key": "official_treasury",
                "source_wallet_address": req["source_wallet_address"],
                "destination_wallet_address": req["destination_wallet_address"],
                "request_uuid": req["request_uuid"],
                "pending_request_uuid": req["request_uuid"],
                "tx_group_hash": req["tx_group_hash"],
                "destination_unowned": bool(self._transfer_request_destination_unowned(req)),
                "settlement_rail": req["settlement_rail"] or "internal_hot_wallet",
                "chain_required": bool(int(req["chain_required"] if req["chain_required"] is not None else 0)),
                "approval_required": bool(int(req["approval_required"] if req["approval_required"] is not None else 0)),
                "network_fee_points": int(req["network_fee_points"] if req["network_fee_points"] is not None else req["fee_points"] or 0),
                "service_fee_points": int(req["service_fee_points"] or 0),
                "memo": req["memo"] or "",
            }
            in_row = None
            if destination_wallet and destination_wallet["user_id"]:
                in_row, _ = self._record_transaction(
                    conn,
                    user_id=recipient_user_id,
                    currency_type=DISPLAY_CURRENCY,
                    direction="credit",
                    amount=int(req["amount_points"]),
                    action_type="official_wallet_grant",
                    reference_type="official_wallet_grant",
                    reference_id=req["tx_group_hash"],
                    idempotency_key=f"official_wallet_grant:{req['request_uuid']}",
                    reason=public_currency_text(req["memo"] or "official wallet grant")[:240],
                    public_metadata=common,
                    actor=actor,
                )
            else:
                bootstrap_economy_layer(conn, chain_secret=self.chain_secret, actor={"role": "system", "id": None}, chain_branch=req_branch)
                append_economy_event(
                    conn,
                    chain_secret=self.chain_secret,
                    event_type="official_wallet_grant",
                    transaction_type="official_wallet_grant",
                    source_fund_key="official_treasury",
                    destination_fund_key=None,
                    destination_address=req["destination_wallet_address"],
                    amount=int(req["amount_points"]),
                    idempotency_key=f"official_wallet_grant:{req['request_uuid']}:unowned_destination",
                    metadata={
                        **common,
                        "destination_unowned": True,
                        "walletization_phase": "1B",
                        "financial_source_of_truth": "points_economy_events",
                    },
                    actor=actor,
                    chain_branch=req_branch,
                )
                conn.execute(
                    """
                    UPDATE points_chain_transfer_requests
                    SET destination_unowned=1
                    WHERE request_uuid=?
                    """,
                    (req["request_uuid"],),
                )
            conn.execute(
                """
                UPDATE points_chain_transfer_requests
                SET transfer_in_ledger_uuid=?, status='confirmed'
                WHERE request_uuid=? AND status='pending'
                """,
                (in_row["ledger_uuid"] if in_row else None, req["request_uuid"]),
            )
            self._refresh_wallet_identity_pending_for_address_locked(
                conn,
                req.get("source_wallet_address"),
                branch=req_branch,
            )
            finalized = conn.execute(
                "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
                (req["request_uuid"],),
            ).fetchone()
            self._audit_log(
                conn,
                "OFFICIAL_WALLET_GRANT",
                "info",
                f"official treasury grant {int(req['amount_points'])} {DISPLAY_CURRENCY}",
                actor=actor,
                target_user_id=recipient_user_id if destination_wallet and destination_wallet["user_id"] else None,
                ledger_id=in_row["id"] if in_row else None,
                metadata={
                    "destination_wallet_address": req["destination_wallet_address"],
                    "transaction_hash": req["tx_group_hash"],
                    "ledger_hash": in_row["ledger_hash"] if in_row else "",
                    "request_uuid": req["request_uuid"],
                    "destination_unowned": not bool(destination_wallet and destination_wallet["user_id"]),
                    "finalized": True,
                },
            )
            self._notify_wallet_transfer_completed(conn, finalized)
            return finalized
        source_wallet = self._wallet_identity_row_for_user_address(
            conn,
            int(req["sender_user_id"]),
            req["source_wallet_address"],
            active_only=True,
        )
        destination_wallet = self._wallet_identity_row_for_user_address(
            conn,
            self._transfer_request_recipient_user_id(req),
            req["destination_wallet_address"],
            active_only=True,
        )
        if not source_wallet:
            return self._fail_transfer_request_locked(
                conn,
                req,
                status="failed_inactive_wallet",
                reason="sender wallet is inactive at finality",
            )
        deposit_destination = self._active_deposit_address_for_address(
            conn,
            req["destination_wallet_address"],
            include_rotated=True,
        )
        if deposit_destination:
            recipient_user_id = int(deposit_destination["user_id"] or 0)
            destination_wallet = None
            if recipient_user_id != self._transfer_request_recipient_user_id(req) or self._transfer_request_destination_unowned(req):
                conn.execute(
                    """
                    UPDATE points_chain_transfer_requests
                    SET recipient_user_id=?, destination_unowned=0
                    WHERE request_uuid=?
                    """,
                    (recipient_user_id, req["request_uuid"]),
                )
                req = dict(conn.execute(
                    "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
                    (req["request_uuid"],),
                ).fetchone())
        else:
            current_destination_owner = self._wallet_identity_owner_for_address(conn, req["destination_wallet_address"])
            if current_destination_owner and current_destination_owner["user_id"]:
                recipient_user_id = int(current_destination_owner["user_id"])
                destination_wallet = current_destination_owner
                if recipient_user_id != self._transfer_request_recipient_user_id(req) or self._transfer_request_destination_unowned(req):
                    conn.execute(
                        """
                        UPDATE points_chain_transfer_requests
                        SET recipient_user_id=?, destination_unowned=0
                        WHERE request_uuid=?
                        """,
                        (recipient_user_id, req["request_uuid"]),
                    )
                    req = dict(conn.execute(
                        "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
                        (req["request_uuid"],),
                    ).fetchone())
            else:
                recipient_user_id = self._transfer_request_recipient_user_id(req)
                destination_wallet = None
                conn.execute(
                    """
                    UPDATE points_chain_transfer_requests
                    SET destination_unowned=1
                    WHERE request_uuid=?
                    """,
                    (req["request_uuid"],),
                )
        total_required = int(req["amount_points"] or 0) + int(req["fee_points"] or 0)
        available = self._wallet_identity_available_for_address(
            conn,
            user_id=int(req["sender_user_id"]),
            address=req["source_wallet_address"],
            exclude_request_uuid=req["request_uuid"],
        )
        if available < total_required:
            return self._fail_transfer_request_locked(
                conn,
                req,
                status="failed_insufficient_balance",
                reason="sender wallet has insufficient balance at finality",
            )
        common = {
            "pending_request_uuid": req["request_uuid"],
            "request_uuid": req["request_uuid"],
            "tx_group_hash": req["tx_group_hash"],
            "source_wallet_address": req["source_wallet_address"],
            "destination_wallet_address": req["destination_wallet_address"],
            "destination_unowned": bool(self._transfer_request_destination_unowned(req)),
            "settlement_rail": req["settlement_rail"] or "cold_chain",
            "chain_required": bool(int(req["chain_required"] if req["chain_required"] is not None else 1)),
            "approval_required": bool(int(req["approval_required"] if req["approval_required"] is not None else 1)),
            "network_fee_points": int(req["network_fee_points"] if req["network_fee_points"] is not None else req["fee_points"] or 0),
            "service_fee_points": int(req["service_fee_points"] or 0),
            "memo": req["memo"] or "",
        }
        out_row, _ = self._record_transaction(
            conn,
            user_id=int(req["sender_user_id"]),
            currency_type=DISPLAY_CURRENCY,
            direction="transfer_out",
            amount=int(req["amount_points"]),
            action_type="wallet_transfer_out",
            reference_type="wallet_transfer",
            reference_id=req["tx_group_hash"],
            idempotency_key=f"wallet_transfer:{req['request_uuid']}:out",
            reason="wallet transfer",
            public_metadata={**common, "to_wallet_address": req["destination_wallet_address"]},
            actor=actor,
        )
        in_row = None
        if destination_wallet and destination_wallet["user_id"]:
            in_row, _ = self._record_transaction(
                conn,
                user_id=recipient_user_id,
                currency_type=DISPLAY_CURRENCY,
                direction="transfer_in",
                amount=int(req["amount_points"]),
                action_type="wallet_transfer_in",
                reference_type="wallet_transfer",
                reference_id=req["tx_group_hash"],
                idempotency_key=f"wallet_transfer:{req['request_uuid']}:in",
                reason="wallet transfer",
                public_metadata={**common, "from_wallet_address": req["source_wallet_address"]},
                actor=actor,
            )
        fee_row = None
        if int(req["fee_points"] or 0):
            fee_row, _ = self._record_transaction(
                conn,
                user_id=int(req["sender_user_id"]),
                currency_type=DISPLAY_CURRENCY,
                direction="debit",
                amount=int(req["fee_points"]),
                action_type="wallet_transfer_fee",
                reference_type="wallet_transfer",
                reference_id=req["tx_group_hash"],
                idempotency_key=f"wallet_transfer:{req['request_uuid']}:fee",
                reason="wallet transfer fee",
                public_metadata={**common, "fee_destination_fund_key": "burn"},
                actor=actor,
            )
        if deposit_destination:
            _bridge, deposit_ledger, _deposit_created = self._credit_confirmed_deposit_transfer_locked(
                conn,
                req,
                deposit_row=deposit_destination,
                actor=actor,
            )
            if deposit_ledger:
                in_row = deposit_ledger
        conn.execute(
            """
            UPDATE points_chain_transfer_requests
            SET transfer_out_ledger_uuid=?, transfer_in_ledger_uuid=?, fee_ledger_uuid=?, status='confirmed'
            WHERE request_uuid=? AND status='pending'
            """,
            (
                out_row["ledger_uuid"],
                in_row["ledger_uuid"] if in_row else None,
                fee_row["ledger_uuid"] if fee_row else None,
                req["request_uuid"],
            ),
        )
        self._refresh_wallet_identity_pending_for_address_locked(
            conn,
            req.get("source_wallet_address"),
            branch=req_branch,
        )
        finalized = conn.execute(
            "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
            (req["request_uuid"],),
        ).fetchone()
        self._notify_wallet_transfer_completed(conn, finalized)
        return finalized

    def _finalize_proved_pending_transfer_requests_locked(self, conn, *, actor=None, limit=1000):
        limit = min(5000, max(1, int(limit or 1000)))
        rows = conn.execute(
            """
            SELECT *
            FROM points_chain_transfer_requests
            WHERE status='pending'
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        finalized_count = 0
        confirmed_count = 0
        failed_count = 0
        finalization_errors = []
        for row in rows:
            before = str(row["status"] or "")
            current = self._maybe_finalize_transfer_request_or_mark_failed_locked(
                conn,
                row,
                actor=actor,
                finalization_errors=finalization_errors,
            )
            after = str(current["status"] or "") if current else before
            if before == "pending" and after != "pending":
                finalized_count += 1
                if after == "confirmed":
                    confirmed_count += 1
                elif after.startswith("failed"):
                    failed_count += 1
        deposit_reconcile = self._reconcile_confirmed_deposit_transfers_locked(conn, actor=actor, limit=limit)
        return {
            "checked_count": len(rows),
            "finalized_count": finalized_count,
            "confirmed_count": confirmed_count,
            "failed_count": failed_count,
            "deposit_bridge_checked_count": int(deposit_reconcile.get("checked_count") or 0),
            "deposit_bridge_credited_count": int(deposit_reconcile.get("credited_count") or 0),
            "deposit_bridge_linked_count": int(deposit_reconcile.get("linked_count") or 0),
            "finalization_errors": finalization_errors,
            "finalization_error_count": len(finalization_errors),
        }

    def _maybe_finalize_transfer_request_or_mark_failed_locked(self, conn, req, *, actor=None, finalization_errors=None):
        savepoint = f"sp_finalize_transfer_{uuid.uuid4().hex}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            current = self._maybe_finalize_transfer_request_locked(conn, req, actor=actor)
            conn.execute(f"RELEASE {savepoint}")
            return current
        except ValueError as exc:
            reason = str(exc)
            try:
                conn.execute(f"ROLLBACK TO {savepoint}")
            finally:
                conn.execute(f"RELEASE {savepoint}")
            if reason != "source fund balance is insufficient":
                raise
            failed = self._fail_transfer_request_locked(
                conn,
                req,
                status="failed_source_fund_insufficient",
                reason=reason,
            )
            item = {
                "request_uuid": req["request_uuid"],
                "tx_group_hash": req["tx_group_hash"],
                "chain_branch": req["chain_branch"] if "chain_branch" in req.keys() else self._main_branch_uuid(),
                "reason": reason,
                "status": failed["status"] if failed else "failed_source_fund_insufficient",
            }
            if isinstance(finalization_errors, list):
                finalization_errors.append(item)
            self._audit_log(
                conn,
                "POINTS_TRANSFER_FINALIZATION_FAILED",
                "warning",
                "pending PointsChain transfer failed during proved finalization",
                actor=actor,
                metadata=item,
            )
            return failed
        except Exception:
            try:
                conn.execute(f"ROLLBACK TO {savepoint}")
            finally:
                conn.execute(f"RELEASE {savepoint}")
            raise

    def submit_wallet_transaction(
        self,
        *,
        actor,
        source_wallet_address,
        destination_wallet_address,
        amount_points,
        fee_points=None,
        request_uuid=None,
        memo="",
        signature=None,
    ):
        actor_id = int(actor_value(actor, "id") or 0)
        if actor_id <= 0:
            raise PermissionError("login required")
        amount = int(amount_points or 0)
        if amount <= 0:
            raise ValueError("amount_points must be positive")
        fee = int(fee_points if fee_points not in (None, "") else max(1, amount // 1000))
        if fee < 0:
            raise ValueError("fee_points must be >= 0")
        source = str(source_wallet_address or "").strip().lower()
        destination = str(destination_wallet_address or "").strip().lower()
        if source == destination:
            raise ValueError("source and destination wallets must differ")
        destination_fund_key_hint = self._explorer_fund_key_for_address(destination)
        if not WALLET_ADDRESS_RE.fullmatch(source):
            raise ValueError("source wallet address format is invalid")
        if not destination_fund_key_hint and not WALLET_ADDRESS_RE.fullmatch(destination):
            raise ValueError("destination wallet address format is invalid")
        if destination_fund_key_hint == "mint":
            raise ValueError("mint system address is a source-only accounting address")
        source_pc0 = is_pc0_internal_address(source)
        destination_pc0 = is_pc0_internal_address(destination)
        if destination_pc0 and not source_pc0:
            raise ValueError(
                "pc0 official hot wallets are internal ledger addresses; external or cold-chain deposits must use a platform deposit address"
            )
        if destination_fund_key_hint == "burn" and not source_pc0:
            raise ValueError("burn system address can only receive internal pc0 debit flows in this release")
        if source_pc0 and destination_pc0:
            fee = 0
            settlement_rail = "internal_hot_wallet"
            chain_required = False
            approval_required = False
        elif source_pc0 and destination_fund_key_hint == "burn":
            fee = 0
            settlement_rail = "internal_system_burn"
            chain_required = False
            approval_required = False
        elif source_pc0:
            settlement_rail = "withdrawal_bridge_lock"
            chain_required = True
            approval_required = True
        else:
            settlement_rail = "cold_chain"
            chain_required = True
            approval_required = True
        request_uuid = str(request_uuid or uuid.uuid4()).strip()[:120]
        if not request_uuid:
            raise ValueError("request_uuid required")
        payload = {
            "source_wallet_address": source,
            "destination_wallet_address": destination,
            "amount_points": amount,
            "fee_points": fee,
            "network_fee_points": fee if chain_required else 0,
            "service_fee_points": 0,
            "settlement_rail": settlement_rail,
            "chain_required": chain_required,
            "approval_required": approval_required,
            "memo": str(memo or "")[:240],
            "transaction_type": "wallet_transfer",
        }
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            active_branch = self._canonical_branch_uuid(conn)
            self._assert_canonical_write_branch(conn, active_branch)
            payload["chain_branch"] = active_branch
            request_hash = self._transfer_request_payload_hash(payload)
            tx_group_hash = sha256_text(f"points-chain-transfer:{active_branch}:{request_uuid}:{request_hash}")
            existing = conn.execute(
                "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
                (request_uuid,),
            ).fetchone()
            if existing:
                if str(existing["chain_branch"] if "chain_branch" in existing.keys() else "main") != active_branch:
                    raise ValueError("transaction idempotency key belongs to a non-canonical branch")
                if existing["request_hash"] != request_hash:
                    raise ValueError("transaction idempotency key conflict")
                existing = self._maybe_finalize_transfer_request_locked(conn, existing, actor=actor)
                conn.commit()
                return {
                    "ok": True,
                    "created": False,
                    "tx_group_hash": existing["tx_group_hash"],
                    "transaction_hash": existing["tx_group_hash"],
                    "transaction": self._transfer_request_public_payload(conn, existing),
                    **self._transfer_request_ledgers(conn, request_uuid),
                }
            source_wallet = self._wallet_identity_row_for_user_address(conn, actor_id, source, active_only=True)
            destination_wallet = None if destination_fund_key_hint in {"mint", "burn"} else self._wallet_identity_owner_for_address(conn, destination)
            if not source_wallet or int(source_wallet["user_id"] or 0) != actor_id:
                raise PermissionError("source wallet does not belong to current user")
            if source_wallet["wallet_type"] in {"mint", "burn"} or source_wallet["custody_mode"] == "system":
                raise PermissionError("system wallets cannot be spent by user transaction")
            self._assert_wallet_identity_can_spend(source_wallet, context="wallet transfer")
            source_freeze = self._address_freeze_locked(conn, source)
            if source_freeze:
                raise PermissionError("source wallet is frozen by governance vote")
            provisional_freeze = self._address_provisional_freeze_locked(conn, source)
            if provisional_freeze:
                raise PermissionError("source wallet is temporarily frozen pending governance review")
            if source_wallet["custody_mode"] == "self_custody":
                if not str(signature or "").strip():
                    raise PermissionError("self-custody wallet transaction signature required")
                verify_wallet_transaction_signature(
                    user_id=actor_id,
                    source_wallet_address=source,
                    destination_wallet_address=destination,
                    amount_points=amount,
                    fee_points=fee,
                    request_uuid=request_uuid,
                    memo=str(memo or "")[:240],
                    public_key_jwk=_json_loads(source_wallet["public_key_jwk_json"], {}),
                    signature=str(signature or ""),
                    chain_branch=active_branch,
                    action_type="points_wallet_transfer",
                    signer_key_id=str(source_wallet["public_key_hash"] or ""),
                )
            destination_unowned = 0
            recipient_user_id = actor_id
            destination_fund_key = destination_fund_key_hint
            destination_deposit = None if destination_fund_key else self._active_deposit_address_for_address(conn, destination)
            if destination_fund_key:
                recipient_user_id = actor_id
            elif destination_deposit:
                recipient_user_id = int(destination_deposit["user_id"] or 0)
                destination_unowned = 0
            elif destination_wallet and destination_wallet["user_id"]:
                recipient_user_id = int(destination_wallet["user_id"])
            else:
                destination_unowned = 1
            if settlement_rail in {"internal_hot_wallet", "internal_system_burn"} and destination_unowned:
                raise ValueError("destination pc0 official hot wallet must be a platform-created internal wallet")
            if destination_wallet and int(destination_wallet["user_id"] or 0) == actor_id and source == destination:
                raise ValueError("source and destination wallets must differ")
            source_available = self._wallet_identity_available_for_address(conn, user_id=actor_id, address=source)
            if source_available < amount + fee:
                raise ValueError("insufficient balance for pending wallet transaction")
            if settlement_rail in {"internal_hot_wallet", "internal_system_burn"}:
                conn.execute(
                    """
                    INSERT INTO points_chain_transfer_requests (
                        request_uuid, chain_branch, request_hash, tx_group_hash, sender_user_id, recipient_user_id,
                        source_wallet_address, destination_wallet_address, destination_unowned, amount_points, fee_points,
                        transaction_type, source_fund_key, memo, settlement_rail, chain_required, approval_required,
                        network_fee_points, service_fee_points,
                        transfer_out_ledger_uuid, transfer_in_ledger_uuid, fee_ledger_uuid, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, 'wallet_transfer', '', ?, ?, 0, 0, 0, 0, NULL, NULL, NULL, 'confirmed', ?)
                    """,
                    (
                        request_uuid,
                        active_branch,
                        request_hash,
                        tx_group_hash,
                        actor_id,
                        recipient_user_id,
                        source,
                        destination,
                        amount,
                        str(memo or "")[:240],
                        settlement_rail,
                        utc_now(),
                    ),
                )
                transfer_metadata = {
                    "pending_request_uuid": request_uuid,
                    "request_uuid": request_uuid,
                    "tx_group_hash": tx_group_hash,
                    "source_wallet_address": source,
                    "destination_wallet_address": destination,
                    "destination_fund_key": destination_fund_key or "",
                    "settlement_rail": settlement_rail,
                    "chain_required": False,
                    "approval_required": False,
                    "network_fee_points": 0,
                    "service_fee_points": 0,
                    "memo": str(memo or "")[:240],
                }
                out_row, _out_created = self._record_transaction(
                    conn,
                    user_id=actor_id,
                    currency_type=DISPLAY_CURRENCY,
                    direction="transfer_out",
                    amount=amount,
                    action_type="wallet_transfer_out",
                    reference_type="wallet_transfer",
                    reference_id=tx_group_hash,
                    idempotency_key=f"wallet_transfer:{request_uuid}:out",
                    reason="INTERNAL_SYSTEM_BURN_OUT" if settlement_rail == "internal_system_burn" else "INTERNAL_PC0_TRANSFER_OUT",
                    public_metadata=transfer_metadata,
                    actor=actor,
                )
                in_row = None
                if not destination_fund_key:
                    in_row, _in_created = self._record_transaction(
                        conn,
                        user_id=recipient_user_id,
                        currency_type=DISPLAY_CURRENCY,
                        direction="transfer_in",
                        amount=amount,
                        action_type="wallet_transfer_in",
                        reference_type="wallet_transfer",
                        reference_id=tx_group_hash,
                        idempotency_key=f"wallet_transfer:{request_uuid}:in",
                        reason="INTERNAL_PC0_TRANSFER_IN",
                        public_metadata=transfer_metadata,
                        actor=actor,
                    )
                conn.execute(
                    """
                    UPDATE points_chain_transfer_requests
                    SET transfer_out_ledger_uuid=?, transfer_in_ledger_uuid=?
                    WHERE request_uuid=?
                    """,
                    (out_row["ledger_uuid"], in_row["ledger_uuid"] if in_row else "", request_uuid),
                )
                req = conn.execute(
                    "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
                    (request_uuid,),
                ).fetchone()
                notification_status = self._notify_wallet_transfer_completed(conn, req)
                warnings = [] if notification_status.get("all_sent") else ["notification_delivery_failed"]
                conn.commit()
                return {
                    "ok": True,
                    "created": True,
                    "warnings": warnings,
                    "notifications": {"completed": notification_status},
                    "tx_group_hash": tx_group_hash,
                    "transaction_hash": tx_group_hash,
                    "transaction": self._transfer_request_public_payload(conn, req),
                    **self._transfer_request_ledgers(conn, request_uuid),
                }
            conn.execute(
                """
                INSERT INTO points_chain_transfer_requests (
                    request_uuid, chain_branch, request_hash, tx_group_hash, sender_user_id, recipient_user_id,
                    source_wallet_address, destination_wallet_address, destination_unowned, amount_points, fee_points,
                    transaction_type, source_fund_key, memo, settlement_rail, chain_required, approval_required,
                    network_fee_points, service_fee_points,
                    transfer_out_ledger_uuid, transfer_in_ledger_uuid, fee_ledger_uuid, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'wallet_transfer', '', ?, ?, ?, ?, ?, 0, NULL, NULL, NULL, 'pending', ?)
                """,
                (
                    request_uuid,
                    active_branch,
                    request_hash,
                    tx_group_hash,
                    actor_id,
                    recipient_user_id,
                    source,
                    destination,
                    destination_unowned,
                    amount,
                    fee,
                    str(memo or "")[:240],
                    settlement_rail,
                    1 if chain_required else 0,
                    1 if approval_required else 0,
                    fee if chain_required else 0,
                    utc_now(),
                ),
            )
            self._refresh_wallet_identity_pending_for_address_locked(conn, source, branch=active_branch)
            req = conn.execute(
                "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
                (request_uuid,),
            ).fetchone()
            notification_status = self._notify_wallet_transfer_pending(conn, req)
            warnings = [] if notification_status.get("all_sent") else ["notification_delivery_failed"]
            conn.commit()
            return {
                "ok": True,
                "created": True,
                "warnings": warnings,
                "notifications": {"pending": notification_status},
                "tx_group_hash": tx_group_hash,
                "transaction_hash": tx_group_hash,
                "transaction": self._transfer_request_public_payload(conn, req),
                **self._transfer_request_ledgers(conn, request_uuid),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_wallet_transactions(self, *, user_id, limit=50, actor=None, run_maintenance=True):
        viewer_id = int(user_id)
        limit = min(100, max(1, int(limit or 50)))
        root_view = actor_value(actor, "username") == "root"
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            safe_mode = self._safe_mode_status(conn)
            finalization_paused = bool(safe_mode.get("safe_mode"))
            sweep = {"checked_count": 0, "finalized_count": 0, "confirmed_count": 0, "failed_count": 0}
            deposit_reconcile = {"checked_count": 0, "credited_count": 0, "linked_count": 0, "skipped_count": 0}
            if run_maintenance and not finalization_paused:
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                if root_view:
                    sweep = self._finalize_proved_pending_transfer_requests_locked(
                        conn,
                        actor=actor,
                        limit=ROOT_TRANSACTION_LIST_SWEEP_LIMIT,
                    )
                deposit_reconcile = self._reconcile_confirmed_deposit_transfers_locked(
                    conn,
                    actor=actor,
                    limit=ROOT_TRANSACTION_LIST_SWEEP_LIMIT,
                )
            if root_view and run_maintenance:
                self._record_transfer_compact_sweep_observability(
                    source="root_transactions_full",
                    sweep=sweep,
                    deposit_reconcile=deposit_reconcile,
                    finalization_paused=finalization_paused,
                )
            if root_view:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM points_chain_transfer_requests
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                identity_state = self._wallet_identity_state_for_user(conn, viewer_id)
                viewer_addresses = sorted(str(item or "").strip().lower() for item in (identity_state.get("addresses") or set()) if item)
                wallet_clause = ""
                params = [viewer_id, viewer_id]
                if viewer_addresses:
                    placeholders = ", ".join("?" for _ in viewer_addresses)
                    wallet_clause = f" OR source_wallet_address IN ({placeholders}) OR destination_wallet_address IN ({placeholders})"
                    params.extend(viewer_addresses)
                    params.extend(viewer_addresses)
                params.append(limit)
                rows = conn.execute(
                    f"""
                    SELECT *
                    FROM points_chain_transfer_requests
                    WHERE sender_user_id=? OR recipient_user_id=?{wallet_clause}
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    tuple(params),
                ).fetchall()
            finalized_count = 0
            refreshed = []
            row_finalization_errors = []
            for row in rows:
                before = str(row["status"] or "")
                current = row if finalization_paused or not run_maintenance else self._maybe_finalize_transfer_request_or_mark_failed_locked(
                    conn,
                    row,
                    actor=actor,
                    finalization_errors=row_finalization_errors,
                )
                if before == "pending" and str(current["status"] or "") != "pending":
                    finalized_count += 1
                refreshed.append(current)
            conn.commit()
            transactions = [
                self._transfer_request_member_summary(
                    conn,
                    row,
                    user_id=viewer_id,
                    root_view=root_view,
                    viewer_addresses=viewer_addresses if not root_view else None,
                )
                for row in refreshed
            ]
            if root_view:
                pending_incoming = 0
                pending_outgoing = sum(
                    int(item["amount_points"] or 0) + int(item["fee_points"] or 0)
                    for item in transactions
                    if item["status"] == "pending"
                )
            else:
                pending_incoming = sum(
                    int(item["amount_points"] or 0)
                    for item in transactions
                    if item["direction"] in {"incoming", "self"} and item["status"] == "pending"
                )
                pending_outgoing = sum(
                    int(item["amount_points"] or 0) + int(item["fee_points"] or 0)
                    for item in transactions
                    if item["direction"] in {"outgoing", "self"} and item["status"] == "pending"
                )
            payload = {
                "ok": True,
                "transactions": transactions,
                "summary": {
                    "count": len(transactions),
                    "pending_count": sum(1 for item in transactions if item["status"] == "pending"),
                    "confirmed_count": sum(1 for item in transactions if item["status"] == "confirmed"),
                    "failed_count": sum(1 for item in transactions if str(item["status"]).startswith("failed")),
                    "pending_incoming_points": pending_incoming,
                    "pending_outgoing_points": pending_outgoing,
                    "finalized_count": finalized_count + int(sweep.get("finalized_count") or 0),
                    "batch_checked_count": int(sweep.get("checked_count") or 0),
                    "batch_finalized_count": int(sweep.get("finalized_count") or 0),
                    "batch_confirmed_count": int(sweep.get("confirmed_count") or 0),
                    "batch_failed_count": int(sweep.get("failed_count") or 0),
                    "deposit_bridge_checked_count": int(deposit_reconcile.get("checked_count") or 0),
                    "deposit_bridge_credited_count": int(deposit_reconcile.get("credited_count") or 0),
                    "deposit_bridge_linked_count": int(deposit_reconcile.get("linked_count") or 0),
                    "finalization_error_count": int(sweep.get("finalization_error_count") or 0) + len(row_finalization_errors),
                    "maintenance_run": bool(run_maintenance),
                },
            }
            finalization_errors = list(sweep.get("finalization_errors") or []) + row_finalization_errors
            if finalization_errors:
                payload["warnings"] = [
                    {
                        "code": "transfer_finalization_failed",
                        "message": "部分 proved pending 交易因官方來源基金不足已標記失敗；交易列表仍可載入。",
                        "items": finalization_errors[:20],
                    }
                ]
            if finalization_paused:
                payload["warnings"] = [*list(payload.get("warnings") or []), "chain_safe_mode_active_finalization_paused"]
                payload["recovery"] = safe_mode
            if self._governance_is_manager_actor(actor):
                addresses = set()
                for item in transactions:
                    addresses.add(str(item.get("source_wallet_address") or "").strip().lower())
                    addresses.add(str(item.get("destination_wallet_address") or "").strip().lower())
                    flow = item.get("wallet_flow") if isinstance(item.get("wallet_flow"), dict) else {}
                    addresses.add(str(flow.get("source_wallet_address") or "").strip().lower())
                    addresses.add(str(flow.get("destination_wallet_address") or "").strip().lower())
                labels = self._official_hot_wallet_labels_locked(conn, actor=actor, addresses=addresses)
                if labels:
                    payload["official_hot_wallet_labels"] = labels
            return payload
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _record_transfer_compact_sweep_observability(self, *, source, sweep=None, deposit_reconcile=None, finalization_paused=False):
        sweep = sweep or {}
        deposit_reconcile = deposit_reconcile or {}

        def int_value(value):
            try:
                return int(value or 0)
            except Exception:
                return 0

        payload = {
            "source": str(source or "unknown"),
            "finished_at": utc_now(),
            "process_local": True,
            "finalization_paused": bool(finalization_paused),
            "sweep_limit": ROOT_TRANSACTION_LIST_SWEEP_LIMIT,
            "checked_count": int_value(sweep.get("checked_count")),
            "finalized_count": int_value(sweep.get("finalized_count")),
            "confirmed_count": int_value(sweep.get("confirmed_count")),
            "failed_count": int_value(sweep.get("failed_count")),
            "finalization_error_count": int_value(sweep.get("finalization_error_count")),
            "deposit_bridge_checked_count": int_value(deposit_reconcile.get("checked_count")),
            "deposit_bridge_credited_count": int_value(deposit_reconcile.get("credited_count")),
            "deposit_bridge_linked_count": int_value(deposit_reconcile.get("linked_count")),
        }
        with self._observability_lock:
            self._last_transfer_compact_sweep = payload
        return payload

    def transfer_finality_observability_snapshot(self, *, recent_limit=200):
        """Bounded health snapshot for admin dashboards; does not finalize rows."""
        started = _monotonic_time.perf_counter()
        limit = max(25, min(1000, int(recent_limit or 200)))
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            branch = self._canonical_branch_uuid(conn)

            def int_value(value):
                try:
                    return int(value or 0)
                except Exception:
                    return 0

            pending = conn.execute(
                """
                SELECT
                    COUNT(*) AS request_count,
                    COALESCE(SUM(amount_points), 0) AS amount_points,
                    MIN(created_at) AS oldest_created_at,
                    MAX(created_at) AS latest_created_at,
                    CAST(COALESCE((julianday('now') - julianday(MIN(created_at))) * 86400, 0) AS INTEGER) AS oldest_age_seconds
                FROM points_chain_transfer_requests
                WHERE chain_branch=? AND status='pending'
                """,
                (branch,),
            ).fetchone()
            pending_by_rail_rows = conn.execute(
                """
                SELECT settlement_rail, COUNT(*) AS request_count, COALESCE(SUM(amount_points), 0) AS amount_points
                FROM points_chain_transfer_requests
                WHERE chain_branch=? AND status='pending'
                GROUP BY settlement_rail
                ORDER BY request_count DESC
                """,
                (branch,),
            ).fetchall()
            recent_status_rows = conn.execute(
                """
                WITH recent AS (
                    SELECT status, settlement_rail, created_at
                    FROM points_chain_transfer_requests
                    WHERE chain_branch=?
                    ORDER BY id DESC
                    LIMIT ?
                )
                SELECT status, COUNT(*) AS request_count, MAX(created_at) AS latest_at
                FROM recent
                GROUP BY status
                ORDER BY request_count DESC
                """,
                (branch, limit),
            ).fetchall()
            unsealed_sample = conn.execute(
                """
                SELECT COUNT(*) AS ledger_count, MAX(created_at) AS latest_at
                FROM (
                    SELECT id, created_at
                    FROM points_ledger
                    WHERE chain_branch=? AND status='confirmed' AND chain_block_id IS NULL
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (branch, limit),
            ).fetchone()
            latest_block = conn.execute(
                """
                SELECT block_number, block_hash, sealed_at
                FROM points_chain_blocks
                ORDER BY block_number DESC
                LIMIT 1
                """
            ).fetchone()
            safe_mode = self._safe_mode_status(conn)
            with self._observability_lock:
                last_sweep = dict(self._last_transfer_compact_sweep or {})

            pending_count = int_value(pending["request_count"] if pending else 0)
            oldest_age_seconds = int_value(pending["oldest_age_seconds"] if pending else 0)
            unsealed_count = int_value(unsealed_sample["ledger_count"] if unsealed_sample else 0)
            signals = []
            if safe_mode.get("safe_mode"):
                signals.append({
                    "code": "points_chain_safe_mode",
                    "severity": "critical",
                    "detail": safe_mode.get("reason") or "",
                })
            if pending_count > 1000:
                signals.append({
                    "code": "transfer_queue_backlog",
                    "severity": "warning",
                    "detail": f"{pending_count} pending transfer requests",
                })
            if oldest_age_seconds > 3600:
                signals.append({
                    "code": "transfer_queue_oldest_age",
                    "severity": "warning",
                    "detail": f"oldest pending transfer age {oldest_age_seconds}s",
                })
            if unsealed_count >= limit:
                signals.append({
                    "code": "unsealed_ledger_sample_limit_reached",
                    "severity": "warning",
                    "detail": f"latest {limit} confirmed ledger rows remain unsealed",
                })
            status = "critical" if any(item.get("severity") == "critical" for item in signals) else "warning" if signals else "ok"
            conn.commit()
            return {
                "ok": status != "critical",
                "status": status,
                "snapshot_type": "points_transfer_finality_observability",
                "generated_at": utc_now(),
                "bounded": True,
                "recent_limit": limit,
                "chain_branch": branch,
                "pending_queue": {
                    "pending_count": pending_count,
                    "amount_points": int_value(pending["amount_points"] if pending else 0),
                    "oldest_created_at": pending["oldest_created_at"] if pending else None,
                    "latest_created_at": pending["latest_created_at"] if pending else None,
                    "oldest_age_seconds": oldest_age_seconds,
                    "by_settlement_rail": {
                        str(row["settlement_rail"] or "unknown"): {
                            "request_count": int_value(row["request_count"]),
                            "amount_points": int_value(row["amount_points"]),
                        }
                        for row in pending_by_rail_rows
                    },
                },
                "recent_transfer_sample": {
                    "limit": limit,
                    "status_counts": {
                        str(row["status"] or "unknown"): {
                            "request_count": int_value(row["request_count"]),
                            "latest_at": row["latest_at"],
                        }
                        for row in recent_status_rows
                    },
                },
                "private_chain": {
                    "unsealed_recent_sample_count": unsealed_count,
                    "unsealed_recent_sample_limit": limit,
                    "unsealed_sample_limit_reached": unsealed_count >= limit,
                    "latest_unsealed_at": unsealed_sample["latest_at"] if unsealed_sample else None,
                    "latest_block": dict(latest_block) if latest_block else {},
                },
                "compact_sweep": {
                    "root_transaction_list_sweep_limit": ROOT_TRANSACTION_LIST_SWEEP_LIMIT,
                    "last_process_local_sweep": last_sweep,
                    "maintenance_from_health_endpoint": False,
                },
                "safe_mode": safe_mode,
                "signals": signals,
                "management_timing": {
                    "total_ms": round((_monotonic_time.perf_counter() - started) * 1000, 3),
                    "bounded": True,
                },
            }
        finally:
            conn.close()

    def run_transfer_finality_sweep(self, *, actor=None, limit=ROOT_TRANSACTION_LIST_SWEEP_LIMIT):
        started = _monotonic_time.perf_counter()
        try:
            sweep_limit = int(limit or ROOT_TRANSACTION_LIST_SWEEP_LIMIT)
        except Exception:
            sweep_limit = ROOT_TRANSACTION_LIST_SWEEP_LIMIT
        sweep_limit = max(1, min(500, sweep_limit))
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            safe_mode = self._safe_mode_status(conn)
            finalization_paused = bool(safe_mode.get("safe_mode"))
            sweep = {"checked_count": 0, "finalized_count": 0, "confirmed_count": 0, "failed_count": 0}
            deposit_reconcile = {"checked_count": 0, "credited_count": 0, "linked_count": 0, "skipped_count": 0}
            if not finalization_paused:
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                sweep = self._finalize_proved_pending_transfer_requests_locked(
                    conn,
                    actor=actor,
                    limit=sweep_limit,
                )
                deposit_reconcile = self._reconcile_confirmed_deposit_transfers_locked(
                    conn,
                    actor=actor,
                    limit=sweep_limit,
                )
                conn.commit()
            marker = self._record_transfer_compact_sweep_observability(
                source="root_finality_sweep_job",
                sweep=sweep,
                deposit_reconcile=deposit_reconcile,
                finalization_paused=finalization_paused,
            )
            result = {
                "ok": not finalization_paused,
                "sweep_type": "points_transfer_finality_sweep",
                "bounded": True,
                "limit": sweep_limit,
                "generated_at": utc_now(),
                "finalization_paused": finalization_paused,
                "safe_mode": safe_mode,
                "sweep": {
                    "checked_count": int(sweep.get("checked_count") or 0),
                    "finalized_count": int(sweep.get("finalized_count") or 0),
                    "confirmed_count": int(sweep.get("confirmed_count") or 0),
                    "failed_count": int(sweep.get("failed_count") or 0),
                    "finalization_error_count": int(sweep.get("finalization_error_count") or 0),
                    "finalization_errors": list(sweep.get("finalization_errors") or [])[:20],
                },
                "deposit_bridge": {
                    "checked_count": int(deposit_reconcile.get("checked_count") or 0),
                    "credited_count": int(deposit_reconcile.get("credited_count") or 0),
                    "linked_count": int(deposit_reconcile.get("linked_count") or 0),
                    "skipped_count": int(deposit_reconcile.get("skipped_count") or 0),
                },
                "observability_marker": marker,
                "management_timing": {
                    "total_ms": round((_monotonic_time.perf_counter() - started) * 1000, 3),
                    "bounded": True,
                },
            }
            if finalization_paused:
                result["warnings"] = ["chain_safe_mode_active_finalization_paused"]
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_wallet_transactions_compact(self, *, user_id, limit=50, actor=None, cursor=None, run_root_sweep=True):
        viewer_id = int(user_id)
        limit = min(100, max(1, int(limit or 50)))
        root_view = actor_value(actor, "username") == "root"
        cursor_id = 0
        try:
            cursor_id = max(0, int(cursor or 0))
        except Exception:
            cursor_id = 0
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            safe_mode = self._safe_mode_status(conn)
            finalization_paused = bool(safe_mode.get("safe_mode"))
            sweep = {"checked_count": 0, "finalized_count": 0, "confirmed_count": 0, "failed_count": 0}
            deposit_reconcile = {"checked_count": 0, "credited_count": 0, "linked_count": 0}
            if root_view and run_root_sweep and not finalization_paused:
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                sweep = self._finalize_proved_pending_transfer_requests_locked(
                    conn,
                    actor=actor,
                    limit=ROOT_TRANSACTION_LIST_SWEEP_LIMIT,
                )
                deposit_reconcile = self._reconcile_confirmed_deposit_transfers_locked(
                    conn,
                    actor=actor,
                    limit=ROOT_TRANSACTION_LIST_SWEEP_LIMIT,
                )
                conn.commit()
            if root_view and run_root_sweep:
                self._record_transfer_compact_sweep_observability(
                    source="root_transactions_compact",
                    sweep=sweep,
                    deposit_reconcile=deposit_reconcile,
                    finalization_paused=finalization_paused,
                )
            branch = self._canonical_branch_uuid(conn)
            where = []
            params = []
            viewer_addresses = set()
            if cursor_id:
                where.append("id<?")
                params.append(cursor_id)
            if root_view:
                where.append("chain_branch=?")
                params.append(branch)
            else:
                identity_state = self._wallet_identity_state_for_user(conn, viewer_id)
                viewer_addresses = {
                    str(item or "").strip().lower()
                    for item in (identity_state.get("addresses") or set())
                    if item
                }
                wallet_clause = ""
                query_params = [viewer_id, viewer_id]
                if viewer_addresses:
                    placeholders = ", ".join("?" for _ in viewer_addresses)
                    wallet_clause = f" OR source_wallet_address IN ({placeholders}) OR destination_wallet_address IN ({placeholders})"
                    query_params.extend(sorted(viewer_addresses))
                    query_params.extend(sorted(viewer_addresses))
                where.append(f"(sender_user_id=? OR recipient_user_id=?{wallet_clause})")
                params.extend(query_params)
                where.append("chain_branch=?")
                params.append(branch)
            sql_where = "WHERE " + " AND ".join(where) if where else ""
            sql_started = _monotonic_time.perf_counter()
            rows = conn.execute(
                f"""
                SELECT *
                FROM points_chain_transfer_requests
                {sql_where}
                ORDER BY id DESC
                LIMIT ?
                """,
                tuple(params + [limit]),
            ).fetchall()
            sql_ms = round((_monotonic_time.perf_counter() - sql_started) * 1000, 3)
            aggregation_started = _monotonic_time.perf_counter()
            transactions = []
            for row in rows:
                req = dict(row)
                sender_id = int(req.get("sender_user_id") or 0)
                recipient_id = self._transfer_request_recipient_user_id(req)
                source_address = str(req.get("source_wallet_address") or "").strip().lower()
                destination_address = str(req.get("destination_wallet_address") or "").strip().lower()
                if root_view:
                    direction = "observed"
                    counterparty = req.get("destination_wallet_address") or ""
                else:
                    source_is_viewer = sender_id == viewer_id or source_address in viewer_addresses
                    destination_is_viewer = recipient_id == viewer_id or destination_address in viewer_addresses
                    if source_is_viewer and destination_is_viewer:
                        direction = "self"
                        counterparty = req.get("destination_wallet_address") or ""
                    elif source_is_viewer:
                        direction = "outgoing"
                        counterparty = req.get("destination_wallet_address") or ""
                    else:
                        direction = "incoming"
                        counterparty = req.get("source_wallet_address") or ""
                status = str(req.get("status") or "pending")
                if status == "confirmed":
                    finality_status = "proved"
                elif status.startswith("failed"):
                    finality_status = "failed"
                else:
                    finality_status = "pending"
                transactions.append({
                    "id": int(req.get("id") or 0),
                    "request_uuid": req.get("request_uuid") or "",
                    "tx_group_hash": req.get("tx_group_hash") or "",
                    "transaction_hash": req.get("tx_group_hash") or "",
                    "transaction_type": req.get("transaction_type") or "wallet_transfer",
                    "settlement_rail": req.get("settlement_rail") or "cold_chain",
                    "chain_required": bool(int(req.get("chain_required") if req.get("chain_required") is not None else 1)),
                    "approval_required": bool(int(req.get("approval_required") if req.get("approval_required") is not None else 1)),
                    "network_fee_points": int(req.get("network_fee_points") if req.get("network_fee_points") is not None else req.get("fee_points") or 0),
                    "service_fee_points": int(req.get("service_fee_points") or 0),
                    "source_fund_key": req.get("source_fund_key") or "",
                    "direction": direction,
                    "status": status,
                    "amount_points": int(req.get("amount_points") or 0),
                    "fee_points": int(req.get("fee_points") or 0),
                    "source_wallet_address": req.get("source_wallet_address") or "",
                    "destination_wallet_address": req.get("destination_wallet_address") or "",
                    "counterparty_wallet_address": counterparty,
                    "memo": req.get("memo") or "",
                    "created_at": req.get("created_at") or "",
                    "finality": {
                        "finality_status": finality_status,
                        "block_status": "snapshot",
                        "snapshot_backed": True,
                    },
                    "wallet_flow": {
                        "source_wallet_address": req.get("source_wallet_address") or "",
                        "destination_wallet_address": req.get("destination_wallet_address") or "",
                        "settlement_rail": req.get("settlement_rail") or "cold_chain",
                        "chain_required": bool(int(req.get("chain_required") if req.get("chain_required") is not None else 1)),
                        "approval_required": bool(int(req.get("approval_required") if req.get("approval_required") is not None else 1)),
                    },
                    "transfer_ledgers": {
                        "transfer_out_ledger_uuid": req.get("transfer_out_ledger_uuid") or "",
                        "transfer_in_ledger_uuid": req.get("transfer_in_ledger_uuid") or "",
                        "fee_ledger_uuid": req.get("fee_ledger_uuid") or "",
                    },
                    "balance_effect": (
                        "confirmed"
                        if status == "confirmed"
                        else "pending_no_recipient_credit"
                        if status == "pending"
                        else "failed_no_balance_change"
                    ),
                })
            pending_incoming = sum(
                int(item["amount_points"] or 0)
                for item in transactions
                if item["direction"] in {"incoming", "self"} and item["status"] == "pending"
            )
            pending_outgoing = sum(
                int(item["amount_points"] or 0) + int(item["fee_points"] or 0)
                for item in transactions
                if (root_view or item["direction"] in {"outgoing", "self"}) and item["status"] == "pending"
            )
            next_cursor = int(transactions[-1]["id"]) if len(transactions) >= limit and transactions else None
            aggregation_ms = round((_monotonic_time.perf_counter() - aggregation_started) * 1000, 3)
            payload = {
                "ok": True,
                "transactions": transactions,
                "summary": {
                    "count": len(transactions),
                    "pending_count": sum(1 for item in transactions if item["status"] == "pending"),
                    "confirmed_count": sum(1 for item in transactions if item["status"] == "confirmed"),
                    "failed_count": sum(1 for item in transactions if str(item["status"]).startswith("failed")),
                    "pending_incoming_points": pending_incoming,
                    "pending_outgoing_points": pending_outgoing,
                    "finalized_count": int(sweep.get("finalized_count") or 0),
                    "batch_checked_count": int(sweep.get("checked_count") or 0),
                    "batch_finalized_count": int(sweep.get("finalized_count") or 0),
                    "batch_confirmed_count": int(sweep.get("confirmed_count") or 0),
                    "batch_failed_count": int(sweep.get("failed_count") or 0),
                    "deposit_bridge_checked_count": int(deposit_reconcile.get("checked_count") or 0),
                    "deposit_bridge_credited_count": int(deposit_reconcile.get("credited_count") or 0),
                    "deposit_bridge_linked_count": int(deposit_reconcile.get("linked_count") or 0),
                    "finalization_error_count": int(sweep.get("finalization_error_count") or 0),
                    "bounded": True,
                    "compact": True,
                    "maintenance_run": bool(root_view and run_root_sweep),
                },
                "cursor": {
                    "next": next_cursor,
                    "limit": limit,
                    "has_more": next_cursor is not None,
                },
                "management_microbenchmark": {
                    "sql_ms": sql_ms,
                    "python_aggregation_ms": aggregation_ms,
                    "rows_returned": len(transactions),
                    "compact": True,
                },
            }
            finalization_errors = list(sweep.get("finalization_errors") or [])
            if finalization_errors:
                payload["warnings"] = [
                    {
                        "code": "transfer_finalization_failed",
                        "message": "部分 proved pending 交易因官方來源基金不足已標記失敗；交易列表仍可載入。",
                        "items": finalization_errors[:20],
                    }
                ]
            if finalization_paused:
                payload["warnings"] = [*list(payload.get("warnings") or []), "chain_safe_mode_active_finalization_paused"]
                payload["recovery"] = safe_mode
            return payload
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def append_trading_reserve_economy_event(
        self,
        conn,
        *,
        reserve_event_uuid,
        delta,
        event_type,
        reason="",
        actor=None,
        source_user_id=None,
        order_id=None,
        fill_id=None,
        points_ledger_uuid=None,
    ):
        delta = int(delta or 0)
        if delta == 0:
            return None, False
        reserve_event_uuid = str(reserve_event_uuid or "").strip()
        if not reserve_event_uuid:
            raise ValueError("reserve event uuid is required")
        event_type = str(event_type or "").strip()
        ignored = {
            "initial_funding",
            "walletized_exchange_fund_alignment",
            # Spot buy/sell fund movement is already represented by the
            # walletized points_ledger rows. Syncing the reserve audit rows as
            # economy events would double count CFD payout/fee flow.
            "spot_cfd_principal_collected",
            "spot_cfd_gross_payout",
            "fee_retained",
        }
        if event_type in ignored:
            return None, False
        amount = abs(delta)
        principal_transfer = event_type in set(EXCHANGE_PRINCIPAL_LENT_TYPES) | set(EXCHANGE_PRINCIPAL_REPAID_TYPES)
        if principal_transfer:
            counterparty_address = exchange_principal_receivable_address(source_user_id)
        else:
            counterparty_address = self._counterparty_user_address(conn, source_user_id) if source_user_id else ""
        if not counterparty_address:
            counterparty_address = f"trading_reserve_event:{reserve_event_uuid}"
        active_branch = self._canonical_branch_uuid(conn)
        bootstrap_economy_layer(conn, chain_secret=self.chain_secret, actor={"role": "system", "id": None}, chain_branch=active_branch)
        metadata = {
            "trading_reserve_event_uuid": reserve_event_uuid,
            "trading_reserve_event_type": event_type,
            "trading_reserve_reason": str(reason or ""),
            "source_user_id": int(source_user_id) if source_user_id else None,
            "order_id": int(order_id) if order_id else None,
            "fill_id": int(fill_id) if fill_id else None,
            "points_ledger_uuid": points_ledger_uuid,
            "walletization_phase": "1B",
            "financial_source_of_truth": "trading_reserve_pool_events",
        }
        if delta > 0:
            if source_user_id and self._economy_external_address_balance(conn, counterparty_address, chain_branch=active_branch) < amount:
                return None, False
            return append_economy_event(
                conn,
                chain_secret=self.chain_secret,
                event_type="trading_reserve_pool_flow",
                transaction_type=event_type,
                source_fund_key=None,
                source_address=counterparty_address,
                destination_fund_key="exchange_fund",
                destination_address=None,
                amount=amount,
                idempotency_key=f"trading_reserve_pool_event:{reserve_event_uuid}",
                metadata=metadata,
                actor=actor,
                chain_branch=active_branch,
            )
        return append_economy_event(
            conn,
            chain_secret=self.chain_secret,
            event_type="trading_reserve_pool_flow",
            transaction_type=event_type,
            source_fund_key="exchange_fund",
            source_address=None,
            destination_fund_key=None,
            destination_address=counterparty_address,
            amount=amount,
            idempotency_key=f"trading_reserve_pool_event:{reserve_event_uuid}",
            metadata=metadata,
            actor=actor,
            chain_branch=active_branch,
        )

    def _sync_exchange_fund_transfer_to_trading_reserve_pool(self, conn, *, req, amount, actor=None):
        if not table_columns(conn, "trading_reserve_pool") or not table_columns(conn, "trading_reserve_pool_events"):
            return None
        event_uuid = f"pointschain_exchange_fund_transfer:{req['request_uuid']}"
        existing = conn.execute(
            "SELECT * FROM trading_reserve_pool_events WHERE event_uuid=?",
            (event_uuid,),
        ).fetchone()
        if existing:
            return existing
        now = utc_now()
        row = conn.execute("SELECT * FROM trading_reserve_pool WHERE id=1").fetchone()
        if not row:
            conn.execute(
                "INSERT INTO trading_reserve_pool (id, balance_points, updated_at, updated_by) VALUES (1, 0, ?, ?)",
                (now, int(actor_value(actor, "id")) if actor_value(actor, "id") else None),
            )
            balance = 0
        else:
            balance = int(row["balance_points"] or 0)
        next_balance = balance + int(amount or 0)
        conn.execute(
            "UPDATE trading_reserve_pool SET balance_points=?, updated_at=?, updated_by=? WHERE id=1",
            (next_balance, now, int(actor_value(actor, "id")) if actor_value(actor, "id") else None),
        )
        conn.execute(
            """
            INSERT INTO trading_reserve_pool_events (
                event_uuid, delta_points, balance_after, event_type, reason,
                actor_user_id, source_user_id, order_id, fill_id, points_ledger_uuid, created_at
            ) VALUES (?, ?, ?, 'official_exchange_fund_replenishment', 'POINTSCHAIN_EXCHANGE_FUND_REPLENISHMENT', ?, NULL, NULL, NULL, NULL, ?)
            """,
            (
                event_uuid,
                int(amount or 0),
                next_balance,
                int(actor_value(actor, "id")) if actor_value(actor, "id") else None,
                now,
            ),
        )
        return conn.execute(
            "SELECT * FROM trading_reserve_pool_events WHERE event_uuid=?",
            (event_uuid,),
        ).fetchone()

    def _backfill_walletized_ledger_events(self, conn, *, limit=1000):
        self.ensure_schema(conn)
        bootstrap_economy_layer(conn, chain_secret=self.chain_secret, actor={"role": "system", "id": None})
        rows = conn.execute(
            """
            SELECT l.*
            FROM points_ledger l
            WHERE l.status='confirmed'
              AND NOT EXISTS (
                SELECT 1 FROM points_economy_events e
                WHERE e.idempotency_key='walletized_ledger:' || l.ledger_uuid
              )
            ORDER BY l.id ASC
            LIMIT ?
            """,
            (max(1, int(limit or 1000)),),
        ).fetchall()
        created = 0
        skipped = 0
        for row in rows:
            event, was_created = self._append_walletized_economy_event(
                conn,
                ledger_row=row,
                public_metadata=_json_loads(row["public_metadata_json"], {}),
                actor={"role": "system", "id": None},
            )
            if event and was_created:
                created += 1
            else:
                skipped += 1
        return {"checked": len(rows), "created": created, "skipped": skipped, "complete": len(rows) < max(1, int(limit or 1000))}

    def _ledger_wallet_flow_for_read(self, conn, row):
        try:
            raw_public_metadata = row["public_metadata_json"]
        except Exception:
            raw_public_metadata = row.get("public_metadata_json") if hasattr(row, "get") else None
        if raw_public_metadata is None and hasattr(row, "get"):
            raw_public_metadata = row.get("public_metadata")
        public_metadata = raw_public_metadata if isinstance(raw_public_metadata, dict) else _json_loads(raw_public_metadata, {})
        snapshot = public_metadata.get("wallet_flow_snapshot") if isinstance(public_metadata, dict) else None
        if isinstance(snapshot, dict):
            flow = dict(snapshot)
        else:
            flow = self._legacy_ledger_wallet_flow_descriptor(conn, row, public_metadata=public_metadata)
        return self._enrich_trading_internal_wallet_flow_for_read(row, flow, public_metadata)

    def _enrich_trading_internal_wallet_flow_for_read(self, row, flow, public_metadata=None):
        action_type = str(row["action_type"] if not isinstance(row, dict) else row.get("action_type") or "")
        if action_type not in {"trading_spot_buy", "trading_spot_sell"}:
            return flow
        enriched = dict(flow or {})
        public_metadata = public_metadata if isinstance(public_metadata, dict) else {}
        exchange_label, exchange_address = self._economy_fund_flow_ref("exchange_fund")
        user_id = row["user_id"] if not isinstance(row, dict) else row.get("user_id")
        user_address = str(
            public_metadata.get("destination_wallet_address")
            or public_metadata.get("source_wallet_address")
            or enriched.get("destination_wallet_address")
            or enriched.get("source_wallet_address")
            or ""
        ).strip().lower()
        if not is_pc0_internal_address(user_address):
            try:
                user_address = official_hot_wallet_address(self.chain_secret, int(user_id))
            except Exception:
                user_address = str(user_address or "")
        user_label = enriched.get("target_wallet_label") or enriched.get("destination_label") or enriched.get("source_label") or "用戶官方熱錢包"
        if action_type == "trading_spot_buy":
            enriched["source_fund_key"] = None
            enriched["destination_fund_key"] = "exchange_fund"
            enriched["source_label"] = enriched.get("source_label") or user_label
            enriched["source_wallet_address"] = enriched.get("source_wallet_address") or user_address
            enriched["destination_label"] = exchange_label
            enriched["destination_wallet_address"] = exchange_address
        else:
            enriched["source_fund_key"] = "exchange_fund"
            enriched["destination_fund_key"] = None
            enriched["source_label"] = exchange_label
            enriched["source_wallet_address"] = exchange_address
            enriched["destination_label"] = enriched.get("destination_label") or user_label
            enriched["destination_wallet_address"] = enriched.get("destination_wallet_address") or user_address
        enriched["settlement_rail"] = "internal_hot_wallet"
        enriched["chain_required"] = False
        enriched["approval_required"] = False
        enriched["network_fee_points"] = 0
        enriched["service_fee_points"] = int(public_metadata.get("service_fee_points") or public_metadata.get("fee") or 0)
        enriched["internal_movement"] = True
        enriched["walletized"] = True
        if not enriched.get("walletization_note") or str(enriched.get("walletization_note") or "").startswith("未分類舊帳本"):
            enriched["walletization_note"] = "交易所 spot 成交使用 pc0 站內帳本，讀取時補齊舊版交易所資金流標籤"
        return enriched

    def _serialize_ledger_for_read(self, conn, row, *, include_user_id=False):
        data = self.serialize_ledger(row, include_user_id=include_user_id)
        data["wallet_flow"] = self._ledger_wallet_flow_for_read(conn, row)
        return data

    def _balance_column(self, currency_type):
        normalize_currency_type(currency_type)
        return "soft_balance"

    def _frozen_column(self, currency_type):
        normalize_currency_type(currency_type)
        return "soft_frozen"

    def _earned_column(self, currency_type):
        normalize_currency_type(currency_type)
        return "total_soft_earned"

    def _spent_column(self, currency_type):
        normalize_currency_type(currency_type)
        return "total_soft_spent"

    def _wallet_statement_deltas(self, *, direction, amount, action_type, public_metadata=None):
        """Return display/reporting income and expense deltas for a ledger row.

        Balance replay still uses the full ledger amount. These deltas drive
        "累計收支", where spot-trade principal is an asset swap and should not
        be counted as user spending/income.
        """
        direction = str(direction or "")
        action = str(action_type or "")
        amount = int(amount or 0)
        metadata = public_metadata if isinstance(public_metadata, dict) else {}

        def meta_int(*keys, default=None):
            for key in keys:
                if key not in metadata or metadata.get(key) is None:
                    continue
                try:
                    return int(metadata.get(key) or 0)
                except Exception:
                    continue
            return default

        if action == "trading_spot_buy":
            return 0, max(0, meta_int("statement_spent_points", "actual_expense_points", default=0) or 0)

        if action == "trading_spot_sell":
            earned = meta_int("statement_earned_points", default=None)
            spent = meta_int("statement_spent_points", default=None)
            if earned is not None or spent is not None:
                return max(0, earned or 0), max(0, spent or 0)
            realized_pnl = meta_int("realized_pnl_points", "net_pnl_points", default=None)
            if realized_pnl is not None:
                return max(0, realized_pnl), max(0, -realized_pnl)
            return 0, 0

        if direction in {"credit", "transfer_in"}:
            return amount, 0
        if direction in {"debit", "transfer_out", "reverse"}:
            return 0, amount
        return 0, 0

    def _last_ledger_hash(self, conn, *, branch_uuid=None):
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        row = conn.execute("SELECT ledger_hash FROM points_ledger WHERE chain_branch=? ORDER BY id DESC LIMIT 1", (branch,)).fetchone()
        return row["ledger_hash"] if row else None

    def _existing_idempotent(self, conn, idempotency_key):
        if not idempotency_key:
            return None
        return conn.execute("SELECT * FROM points_ledger WHERE idempotency_key=?", (idempotency_key,)).fetchone()

    def _admin_account_rows(self, conn):
        cols = table_columns(conn, "users")
        if "id" not in cols or "username" not in cols or "role" not in cols:
            return []
        status_filter = "AND COALESCE(status, 'active')='active'" if "status" in cols else ""
        return conn.execute(
            f"""
            SELECT id, username, role FROM users
            WHERE username<>'root'
              AND role IN ('manager', 'super_admin')
              {status_filter}
            ORDER BY id ASC
            """
        ).fetchall()

    def _genesis_user_account_rows(self, conn, *, default_only=False):
        cols = table_columns(conn, "users")
        if "id" not in cols or "username" not in cols or "role" not in cols:
            return []
        status_filter = "AND COALESCE(status, 'active')='active'" if "status" in cols else ""
        default_filter = "AND username='test'" if default_only else ""
        return conn.execute(
            f"""
            SELECT id, username, role FROM users
            WHERE username<>'root'
              AND role NOT IN ('manager', 'super_admin')
              {default_filter}
              {status_filter}
            ORDER BY id ASC
            """
        ).fetchall()

    def award_signup_bonus_locked(self, conn, *, user_id, actor=None):
        row, created = self._record_transaction(
            conn,
            user_id=user_id,
            currency_type=DISPLAY_CURRENCY,
            direction="credit",
            amount=SIGNUP_BONUS_POINTS,
            action_type="new_user_signup_bonus",
            reference_type="user_registration",
            reference_id=str(user_id),
            idempotency_key=f"new_user_signup_bonus:{int(user_id)}",
            reason="new user signup bonus",
            public_metadata=self._official_hot_wallet_credit_metadata(
                user_id,
                {"grant": "signup_bonus", "amount": SIGNUP_BONUS_POINTS},
            ),
            actor=actor,
        )
        return {
            "ok": True,
            "created": created,
            "ledger": self.serialize_ledger(row, include_user_id=True),
            "wallet": self.wallet_payload_for_read(conn, user_id),
        }

    def award_signup_bonus(self, *, user_id, actor=None):
        return self.record_transaction(
            user_id=user_id,
            currency_type=DISPLAY_CURRENCY,
            direction="credit",
            amount=SIGNUP_BONUS_POINTS,
            action_type="new_user_signup_bonus",
            reference_type="user_registration",
            reference_id=str(user_id),
            idempotency_key=f"new_user_signup_bonus:{int(user_id)}",
            reason="new user signup bonus",
            public_metadata=self._official_hot_wallet_credit_metadata(
                user_id,
                {"grant": "signup_bonus", "amount": SIGNUP_BONUS_POINTS},
            ),
            actor=actor,
        )

    def award_birthday_gift(self, *, user_id, birthday_year, birthday_date=None, actor=None):
        year = int(birthday_year)
        return self.record_transaction(
            user_id=user_id,
            currency_type=DISPLAY_CURRENCY,
            direction="credit",
            amount=BIRTHDAY_GIFT_POINTS,
            action_type="birthday_gift",
            reference_type="birthday_year",
            reference_id=str(year),
            idempotency_key=f"birthday_gift:{year}:{int(user_id)}",
            reason=f"birthday gift {year}",
            public_metadata=self._official_hot_wallet_credit_metadata(
                user_id,
                {
                    "grant": "birthday_gift",
                    "birthday_year": year,
                    "birthday_date": str(birthday_date or ""),
                    "amount": BIRTHDAY_GIFT_POINTS,
                },
            ),
            actor=actor,
        )

    def award_admin_initial_grant(self, *, user_id, actor=None, require_wallet=True):
        deferred = self._initial_grant_ready_or_deferred(
            user_id=user_id,
            expected_action_type="admin_initial_grant",
            require_wallet=require_wallet,
        )
        if deferred:
            return deferred
        return self.record_transaction(
            user_id=user_id,
            currency_type=DISPLAY_CURRENCY,
            direction="credit",
            amount=ADMIN_INITIAL_POINTS,
            action_type="admin_initial_grant",
            reference_type="genesis_admin_allocation",
            reference_id=str(user_id),
            idempotency_key=f"admin_initial_grant:{int(user_id)}",
            reason="admin genesis allocation",
            public_metadata=self._official_hot_wallet_credit_metadata(
                user_id,
                {"grant": "admin_initial", "amount": ADMIN_INITIAL_POINTS},
            ),
            actor=actor,
        )

    def award_user_initial_grant(self, *, user_id, actor=None, require_wallet=True):
        deferred = self._initial_grant_ready_or_deferred(
            user_id=user_id,
            expected_action_type="user_initial_grant",
            require_wallet=require_wallet,
        )
        if deferred:
            return deferred
        return self.record_transaction(
            user_id=user_id,
            currency_type=DISPLAY_CURRENCY,
            direction="credit",
            amount=USER_INITIAL_POINTS,
            action_type="user_initial_grant",
            reference_type="genesis_user_allocation",
            reference_id=str(user_id),
            idempotency_key=f"user_initial_grant:{int(user_id)}",
            reason="user genesis allocation",
            public_metadata=self._official_hot_wallet_credit_metadata(
                user_id,
                {"grant": "user_initial", "amount": USER_INITIAL_POINTS},
            ),
            actor=actor,
        )

    def award_initial_grants_after_wallet_onboarding(self, *, user_id, actor=None):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            status = self.wallet_initial_grant_status(conn, user_id)
        finally:
            conn.close()
        if not status.get("required"):
            return {"ok": True, "created": [], "created_count": 0, "deferred": False, "initial_grant": status}
        if status.get("deferred_until_wallet"):
            return {"ok": True, "created": [], "created_count": 0, "deferred": True, "initial_grant": status}
        if status.get("action_type") == "admin_initial_grant":
            result = self.award_admin_initial_grant(user_id=user_id, actor=actor)
        elif status.get("action_type") == "user_initial_grant":
            result = self.award_user_initial_grant(user_id=user_id, actor=actor)
        else:
            return {"ok": True, "created": [], "created_count": 0, "deferred": False, "initial_grant": status}
        created = [{
            "user_id": int(user_id),
            "grant": status.get("grant") or "",
            "amount": int(status.get("amount") or 0),
            "ledger_hash": (result.get("ledger") or {}).get("ledger_hash", ""),
        }] if result.get("created") else []
        return {
            "ok": True,
            "created": created,
            "created_count": len(created),
            "deferred": bool(result.get("deferred")),
            "initial_grant": (result.get("initial_grant") or status),
            "ledger": result.get("ledger"),
            "wallet": result.get("wallet"),
        }

    def current_salary_week(self):
        year, week, _weekday = datetime.now(timezone.utc).isocalendar()
        return f"{int(year)}-W{int(week):02d}"

    def award_admin_weekly_salary(self, *, user_id, salary_week=None, actor=None):
        week = salary_week or self.current_salary_week()
        return self.record_transaction(
            user_id=user_id,
            currency_type=DISPLAY_CURRENCY,
            direction="credit",
            amount=ADMIN_WEEKLY_SALARY_POINTS,
            action_type="admin_weekly_salary",
            reference_type="admin_salary",
            reference_id=week,
            idempotency_key=f"admin_weekly_salary:{week}:{int(user_id)}",
            reason=f"admin weekly salary {week}",
            public_metadata=self._official_hot_wallet_credit_metadata(
                user_id,
                {"grant": "admin_weekly_salary", "salary_week": week, "amount": ADMIN_WEEKLY_SALARY_POINTS},
            ),
            actor=actor,
        )

    def bootstrap_admin_initial_grants(self, *, actor=None, seal_genesis=True, require_wallet=True):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            admins = [dict(row) for row in self._admin_account_rows(conn)]
            has_blocks = conn.execute("SELECT 1 FROM points_chain_blocks LIMIT 1").fetchone() is not None
            users = [
                dict(row)
                for row in self._genesis_user_account_rows(conn, default_only=True)
            ]
        finally:
            conn.close()
        created = []
        deferred = []
        for admin in admins:
            result = self.award_admin_initial_grant(user_id=admin["id"], actor=actor, require_wallet=require_wallet)
            if result.get("created"):
                created.append({"user_id": admin["id"], "username": admin["username"], "role": admin["role"], "grant": "admin_initial", "amount": ADMIN_INITIAL_POINTS})
            elif result.get("deferred"):
                deferred.append({"user_id": admin["id"], "username": admin["username"], "role": admin["role"], "grant": "admin_initial", "amount": ADMIN_INITIAL_POINTS, "reason": "wallet_required"})
        for user in users:
            result = self.award_user_initial_grant(user_id=user["id"], actor=actor, require_wallet=require_wallet)
            if result.get("created"):
                created.append({"user_id": user["id"], "username": user["username"], "role": user["role"], "grant": "user_initial", "amount": USER_INITIAL_POINTS})
            elif result.get("deferred"):
                deferred.append({"user_id": user["id"], "username": user["username"], "role": user["role"], "grant": "user_initial", "amount": USER_INITIAL_POINTS, "reason": "wallet_required"})
        sealed = None
        if seal_genesis and created and not has_blocks:
            sealed = self.seal_block(actor=actor, limit=500)
        return {"ok": True, "created": created, "created_count": len(created), "deferred": deferred, "deferred_count": len(deferred), "sealed": sealed}

    def award_admin_weekly_salaries(self, *, salary_week=None, actor=None):
        week = salary_week or self.current_salary_week()
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            admins = [dict(row) for row in self._admin_account_rows(conn)]
        finally:
            conn.close()
        created = []
        for admin in admins:
            result = self.award_admin_weekly_salary(user_id=admin["id"], salary_week=week, actor=actor)
            if result.get("created"):
                created.append({"user_id": admin["id"], "username": admin["username"], "role": admin["role"]})
        return {"ok": True, "salary_week": week, "created": created, "created_count": len(created)}

    def _record_transaction(
        self,
        conn,
        *,
        user_id,
        currency_type,
        direction,
        amount,
        action_type,
        reference_type=None,
        reference_id=None,
        idempotency_key=None,
        reason="",
        public_metadata=None,
        private_metadata=None,
        sensitive_metadata_encrypted="",
        actor=None,
        risk_flag="none",
        risk_score=0,
    ):
        self._assert_chain_writable(conn, "ledger transaction")
        currency_type = normalize_currency_type(currency_type)
        if direction not in LEDGER_DIRECTIONS:
            raise ValueError("unsupported ledger direction")
        amount = int(amount)
        if amount <= 0:
            raise ValueError("amount must be positive")
        existing = self._existing_idempotent(conn, idempotency_key)
        if existing:
            return existing, False

        wallet = self.ensure_wallet(conn, user_id)
        create_official_hot_wallet(conn, user_id=int(user_id), chain_secret=self.chain_secret)
        self.ensure_user_deposit_address(conn, user_id)
        if wallet["wallet_status"] == "closed":
            raise ValueError("wallet is closed")
        if wallet["wallet_status"] == "frozen" and direction in {"credit", "debit", "freeze"}:
            raise ValueError("wallet is frozen")
        if wallet["wallet_status"] == "limited" and direction in {"debit", "transfer_out"}:
            raise ValueError("wallet is limited")

        balance_col = self._balance_column(currency_type)
        frozen_col = self._frozen_column(currency_type)
        earned_col = self._earned_column(currency_type)
        spent_col = self._spent_column(currency_type)
        public_metadata, wallet_flow_snapshot = self._ledger_metadata_with_wallet_flow_snapshot(
            conn,
            user_id=user_id,
            direction=direction,
            action_type=action_type,
            public_metadata=public_metadata or {},
        )
        account_balance_before = int(wallet[balance_col])
        account_frozen_before = int(wallet[frozen_col])
        exclude_pending_request_uuid = ""
        if action_type in {"wallet_transfer_out", "wallet_transfer_fee"} and isinstance(public_metadata, dict):
            exclude_pending_request_uuid = str(
                public_metadata.get("pending_request_uuid") or public_metadata.get("request_uuid") or ""
            ).strip()
        active_branch = self._canonical_branch_uuid(conn)
        identity_balances = self._wallet_identity_balances_for_user(
            conn,
            user_id,
            include_pending=False,
            branch_uuid=active_branch,
        )
        available_balance_before = None
        if identity_balances.get("has_identity"):
            identity_balance_map = identity_balances.get("balances") or {}
            account_balance_before = sum(int(item.get("balance") or 0) for item in identity_balance_map.values())
            account_frozen_before = sum(int(item.get("frozen") or 0) for item in identity_balance_map.values())
            ledger_address = ""
            if direction in {"credit", "transfer_in"}:
                ledger_address = str(wallet_flow_snapshot.get("destination_wallet_address") or "")
            elif direction in {"debit", "transfer_out", "reverse", "freeze"}:
                ledger_address = str(wallet_flow_snapshot.get("source_wallet_address") or "")
            elif direction == "unfreeze":
                ledger_address = str(wallet_flow_snapshot.get("source_wallet_address") or wallet_flow_snapshot.get("destination_wallet_address") or "")
            if ledger_address not in identity_balance_map:
                ledger_address = str(identity_balances.get("primary_address") or "")
            if not ledger_address:
                raise ValueError("active wallet identity is required")
            if direction in {"debit", "transfer_out", "reverse", "freeze"}:
                self._assert_address_not_frozen_for_outgoing_locked(
                    conn,
                    ledger_address,
                    action=action_type or "ledger outgoing write",
                )
            active = identity_balance_map.get(ledger_address, {"balance": 0, "frozen": 0})
            balance_before = int(active.get("balance") or 0)
            frozen_before = int(active.get("frozen") or 0)
            if direction in {"debit", "transfer_out", "reverse", "freeze"} and action_type not in {"wallet_transfer_out", "wallet_transfer_fee"}:
                spendable_state = self._wallet_identity_balances_for_user(
                    conn,
                    user_id,
                    include_pending=True,
                    exclude_request_uuid=exclude_pending_request_uuid or None,
                    branch_uuid=active_branch,
                )
                spendable_active = (spendable_state.get("balances") or {}).get(ledger_address, {"balance": balance_before})
                available_balance_before = int(spendable_active.get("balance") or 0)
        else:
            balance_before = account_balance_before
            frozen_before = account_frozen_before
        balance_after = balance_before
        frozen_after = frozen_before
        account_balance_after = account_balance_before
        account_frozen_after = account_frozen_before
        earned_delta = 0
        spent_delta = 0

        if direction in {"credit", "transfer_in"}:
            balance_after += amount
            account_balance_after += amount
        elif direction in {"debit", "transfer_out", "reverse"}:
            if (available_balance_before if available_balance_before is not None else balance_before) < amount:
                raise ValueError("insufficient balance")
            balance_after -= amount
            account_balance_after -= amount
        elif direction == "freeze":
            if (available_balance_before if available_balance_before is not None else balance_before) < amount:
                raise ValueError("insufficient balance")
            balance_after -= amount
            frozen_after += amount
            account_balance_after -= amount
            account_frozen_after += amount
        elif direction == "unfreeze":
            if frozen_before < amount:
                raise ValueError("insufficient frozen balance")
            balance_after += amount
            frozen_after -= amount
            account_balance_after += amount
            account_frozen_after -= amount

        earned_delta, spent_delta = self._wallet_statement_deltas(
            direction=direction,
            amount=amount,
            action_type=action_type,
            public_metadata=public_metadata,
        )
        public_json = _metadata_json_checked(public_metadata, label="public_metadata")
        private_json = _metadata_json_checked(private_metadata or {}, label="private_metadata")
        meta_hash = metadata_hash(public_metadata, private_metadata or {}, sensitive_metadata_encrypted or "")
        now = utc_now()
        ledger_uuid = str(uuid.uuid4())
        ledger_data = {
            "ledger_uuid": ledger_uuid,
            "public_account_id": self._public_account_id(user_id),
            "currency_type": currency_type,
            "direction": direction,
            "amount": amount,
            "balance_before": balance_before,
            "balance_after": balance_after,
            "action_type": action_type,
            "reference_type": reference_type,
            "reference_id": str(reference_id) if reference_id is not None else None,
            "metadata_hash": meta_hash,
            "chain_branch": active_branch,
            "previous_ledger_hash": self._last_ledger_hash(conn, branch_uuid=active_branch),
            "created_at": now,
        }
        ledger_hash = compute_ledger_hash(ledger_data)
        cur = conn.execute(
            """
            INSERT INTO points_ledger (
                ledger_uuid, chain_branch, user_id, public_account_id, currency_type, direction, amount,
                balance_before, balance_after, action_type, reference_type, reference_id,
                idempotency_key, reason, public_metadata_json, private_metadata_json,
                sensitive_metadata_encrypted, metadata_hash, previous_ledger_hash, ledger_hash,
                risk_flag, risk_score, created_by, created_by_role, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)
            """,
            (
                ledger_uuid,
                ledger_data["chain_branch"],
                int(user_id),
                ledger_data["public_account_id"],
                currency_type,
                direction,
                amount,
                balance_before,
                balance_after,
                action_type,
                reference_type,
                ledger_data["reference_id"],
                idempotency_key,
                reason or "",
                public_json,
                private_json,
                sensitive_metadata_encrypted or "",
                meta_hash,
                ledger_data["previous_ledger_hash"],
                ledger_hash,
                risk_flag,
                int(risk_score or 0),
                int(actor_value(actor, "id")) if actor_value(actor, "id") else None,
                actor_value(actor, "role"),
                now,
            ),
        )
        conn.execute(
            f"""
            UPDATE points_wallets
            SET {balance_col}=?, {frozen_col}=?, {earned_col}={earned_col}+?, {spent_col}={spent_col}+?, updated_at=?
            WHERE user_id=?
            """,
            (account_balance_after, account_frozen_after, earned_delta, spent_delta, now, int(user_id)),
        )
        row = conn.execute("SELECT * FROM points_ledger WHERE id=?", (cur.lastrowid,)).fetchone()
        self._apply_wallet_identity_materialized_ledger_delta_locked(
            conn,
            ledger_row=row,
            wallet_flow_snapshot=wallet_flow_snapshot,
        )
        economy_event, economy_created = self._append_walletized_economy_event(
            conn,
            ledger_row=row,
            public_metadata=public_metadata,
            actor=actor,
        )
        self._audit_log(
            conn,
            "LEDGER_APPEND",
            "info",
            f"{direction} {amount} {DISPLAY_CURRENCY} for user {user_id}",
            actor=actor,
            target_user_id=int(user_id),
            ledger_id=row["id"],
            metadata={
                "currency_type": DISPLAY_CURRENCY,
                "action_type": action_type,
                "reference_type": reference_type,
                "reference_id": reference_id,
                "walletized_economy_event_uuid": economy_event["event_uuid"] if economy_event else "",
                "walletized_economy_event_created": bool(economy_created),
                "wallet_flow_snapshot": wallet_flow_snapshot,
            },
        )
        return row, True

    def record_transaction(self, **kwargs):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            row, created = self._record_transaction(conn, **kwargs)
            conn.commit()
            return {"ok": True, "created": created, "ledger": self.serialize_ledger(row, include_user_id=True), "wallet": self.get_wallet(row["user_id"])}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def sanction_wallet(
        self,
        *,
        actor,
        user_id,
        wallet_status=None,
        risk_level=None,
        reason="",
        freeze_amount=0,
        unfreeze_amount=0,
    ):
        raise PermissionError("blockchain_permission_model: direct wallet sanctions are disabled; use governance-voted address freeze, scam label, or emergency branch instead")

    def _rule_for_key(self, conn, rule_key):
        return conn.execute("SELECT * FROM points_rules WHERE rule_key=? AND enabled=1", (rule_key,)).fetchone()

    def earn_points(self, *, user_id, rule_key, reference_type=None, reference_id=None, idempotency_key=None, metadata=None, actor=None):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            self._assert_chain_writable(conn, "earn points")
            rule = self._rule_for_key(conn, rule_key)
            if not rule:
                raise ValueError("points rule not found or disabled")
            if rule["direction"] != "credit":
                raise ValueError("rule is not a credit rule")
            if rule["requires_admin_review"]:
                raise PermissionError("blockchain_permission_model: pending reward review is disabled")
            amount = int(rule["base_amount"])
            self._enforce_rule_limits(conn, user_id=user_id, rule=rule, amount=amount)
            row, created = self._record_transaction(
                conn,
                user_id=user_id,
                currency_type=rule["currency_type"],
                direction="credit",
                amount=amount,
                action_type=rule["action_type"],
                reference_type=reference_type,
                reference_id=reference_id,
                idempotency_key=idempotency_key or f"{rule_key}:{user_id}:{reference_type or ''}:{reference_id or ''}",
                reason=f"rule:{rule_key}",
                public_metadata=metadata or {},
                actor=actor,
            )
            conn.commit()
            return {"ok": True, "created": created, "ledger": self.serialize_ledger(row), "wallet": self.get_wallet(user_id)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _enforce_rule_limits(self, conn, *, user_id, rule, amount):
        today = datetime.now(timezone.utc).date().isoformat()
        if rule["daily_user_limit"]:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS total
                FROM points_ledger
                WHERE user_id=? AND action_type=? AND status='confirmed' AND created_at>=?
                """,
                (int(user_id), rule["action_type"], today),
            ).fetchone()
            if int(row["total"] or 0) + int(amount) > int(rule["daily_user_limit"]):
                raise ValueError("daily user points limit exceeded")
        if rule["cooldown_seconds"]:
            row = conn.execute(
                """
                SELECT created_at FROM points_ledger
                WHERE user_id=? AND action_type=? AND status='confirmed'
                ORDER BY created_at DESC LIMIT 1
                """,
                (int(user_id), rule["action_type"]),
            ).fetchone()
            if row and row["created_at"]:
                try:
                    last = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
                    if elapsed < int(rule["cooldown_seconds"]):
                        raise ValueError("points rule cooldown active")
                except ValueError:
                    raise
                except Exception:
                    pass

    def _serialize_service_fee_charge(self, row):
        if not row:
            return None
        return {
            "id": int(row["id"]),
            "charge_uuid": row["charge_uuid"],
            "chain_branch": row["chain_branch"] if "chain_branch" in row.keys() else self._main_branch_uuid(),
            "user_id": int(row["user_id"]),
            "item_key": row["item_key"],
            "quantity": int(row["quantity"] or 0),
            "amount_points": int(row["amount_points"] or 0),
            "currency_type": display_currency_type(row["currency_type"]),
            "source_wallet_address": row["source_wallet_address"] or "",
            "status": row["status"],
            "idempotency_key": row["idempotency_key"] or "",
            "freeze_ledger_uuid": row["freeze_ledger_uuid"] or "",
            "unfreeze_ledger_uuid": row["unfreeze_ledger_uuid"] or "",
            "debit_ledger_uuid": row["debit_ledger_uuid"] or "",
            "batch_uuid": row["batch_uuid"] or "",
            "reference_type": row["reference_type"] or "",
            "reference_id": row["reference_id"] or "",
            "metadata": _json_loads(row["metadata_json"], {}),
            "created_at": row["created_at"],
            "settled_at": row["settled_at"],
            "cancelled_at": row["cancelled_at"],
        }

    def _service_fee_charge_by_idempotency(self, conn, idempotency_key):
        key = str(idempotency_key or "").strip()
        if not key:
            return None
        return conn.execute(
            "SELECT * FROM points_service_fee_charges WHERE idempotency_key=?",
            (key,),
        ).fetchone()

    def _service_fee_charge_by_uuid(self, conn, charge_uuid):
        charge_uuid = str(charge_uuid or "").strip()
        if not charge_uuid:
            return None
        return conn.execute(
            "SELECT * FROM points_service_fee_charges WHERE charge_uuid=?",
            (charge_uuid,),
        ).fetchone()

    def _service_fee_reserved_rows(self, conn, *, user_id, source_wallet_address=None, chain_branch=None):
        source_address = str(source_wallet_address or "").strip().lower()
        branch = str(chain_branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        return conn.execute(
            """
            SELECT * FROM points_service_fee_charges
            WHERE user_id=? AND source_wallet_address=? AND status='reserved' AND chain_branch=?
            ORDER BY id ASC
            """,
            (int(user_id), source_address, branch),
        ).fetchall()

    def _service_fee_reserved_total(self, conn, *, user_id, source_wallet_address=None, chain_branch=None):
        source_address = str(source_wallet_address or "").strip().lower()
        branch = str(chain_branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        row = conn.execute(
            """
            SELECT COALESCE(SUM(amount_points), 0) AS total
            FROM points_service_fee_charges
            WHERE user_id=? AND source_wallet_address=? AND status='reserved' AND chain_branch=?
            """,
            (int(user_id), source_address, branch),
        ).fetchone()
        return int((row["total"] if row else 0) or 0)

    def _service_fee_existing_response(self, conn, *, charge_row, item=None):
        freeze_row = conn.execute(
            "SELECT * FROM points_ledger WHERE ledger_uuid=?",
            (charge_row["freeze_ledger_uuid"] or "",),
        ).fetchone()
        unfreeze_row = conn.execute(
            "SELECT * FROM points_ledger WHERE ledger_uuid=?",
            (charge_row["unfreeze_ledger_uuid"] or "",),
        ).fetchone()
        debit_row = conn.execute(
            "SELECT * FROM points_ledger WHERE ledger_uuid=?",
            (charge_row["debit_ledger_uuid"] or "",),
        ).fetchone()
        reserved_total = self._service_fee_reserved_total(
            conn,
            user_id=int(charge_row["user_id"]),
            source_wallet_address=charge_row["source_wallet_address"] or "",
            chain_branch=charge_row["chain_branch"] if "chain_branch" in charge_row.keys() else None,
        )
        metadata = _json_loads(charge_row["metadata_json"], {})
        is_internal_settled = (
            str(charge_row["status"] or "") == "settled"
            and debit_row is not None
            and (
                metadata.get("settlement_policy") == "internal_hot_wallet_immediate_debit"
                or str(debit_row["action_type"] or "").startswith("service_fee_internal_debit:")
            )
        )
        if is_internal_settled:
            return {
                "ok": True,
                "created": False,
                "settlement_layer": "internal_hot_wallet_ledger",
                "settlement_policy": "internal_hot_wallet_immediate_debit",
                "charge": self._serialize_service_fee_charge(charge_row),
                "ledger": self._serialize_ledger_for_read(conn, debit_row),
                "settlement": {
                    "created": False,
                    "status": "settled",
                    "settled_amount_points": int(charge_row["amount_points"] or 0),
                    "reason": "pc0_internal_immediate",
                    "debit_ledger": self._serialize_ledger_for_read(conn, debit_row),
                },
                "wallet": self.wallet_payload_for_read(conn, int(charge_row["user_id"])),
                "item": dict(item) if item else None,
            }
        return {
            "ok": True,
            "created": False,
            "settlement_layer": "service_fee_subledger",
            "settlement_policy": "legacy_service_fee_batch_debit",
            "batch_threshold_points": LEGACY_SERVICE_BATCH_SETTLEMENT_MIN_POINTS,
            "charge": self._serialize_service_fee_charge(charge_row),
            "ledger": self.serialize_ledger(freeze_row) if freeze_row else None,
            "settlement": {
                "created": False,
                "status": charge_row["status"],
                "batch_uuid": charge_row["batch_uuid"] or "",
                "reserved_total_points": reserved_total,
                "settled_amount_points": int(charge_row["amount_points"] or 0) if charge_row["status"] == "settled" else 0,
                "unfreeze_ledger": self.serialize_ledger(unfreeze_row) if unfreeze_row else None,
                "debit_ledger": self.serialize_ledger(debit_row) if debit_row else None,
                "threshold_points": LEGACY_SERVICE_BATCH_SETTLEMENT_MIN_POINTS,
            },
            "wallet": self.wallet_payload_for_read(conn, int(charge_row["user_id"])),
            "item": dict(item) if item else None,
        }

    def _verify_service_fee_wallet_signature(self, *, conn, user_id, source_wallet_address, item_key, quantity, amount_points, request_uuid, reference_type, reference_id, signature, chain_branch=None):
        source_address = str(source_wallet_address or "").strip().lower()
        if not source_address:
            return False
        wallet = self._wallet_identity_row_for_user_address(conn, user_id, source_address, active_only=True)
        if not wallet:
            return False
        if str(wallet["custody_mode"] or "") != "self_custody":
            return False
        if not str(signature or "").strip():
            raise PermissionError("self-custody wallet service fee signature required")
        verify_wallet_service_fee_signature(
            user_id=user_id,
            source_wallet_address=source_address,
            item_key=item_key,
            quantity=quantity,
            amount_points=amount_points,
            request_uuid=request_uuid,
            reference_type=reference_type or "",
            reference_id=reference_id or "",
            public_key_jwk=_json_loads(wallet["public_key_jwk_json"], {}),
            signature=signature,
            chain_branch=chain_branch or self._canonical_branch_uuid(conn),
            action_type="points_service_fee_payment",
            signer_key_id=str(wallet["public_key_hash"] or ""),
        )
        return True

    def _settle_service_fee_charges_locked(self, conn, *, user_id, source_wallet_address=None, force=False, actor=None, reason="threshold"):
        source_address = str(source_wallet_address or "").strip().lower()
        active_branch = self._canonical_branch_uuid(conn)
        rows = self._service_fee_reserved_rows(conn, user_id=user_id, source_wallet_address=source_address, chain_branch=active_branch)
        total = sum(int(row["amount_points"] or 0) for row in rows)
        if total <= 0:
            return {
                "created": False,
                "status": "none",
                "reserved_total_points": 0,
                "threshold_points": LEGACY_SERVICE_BATCH_SETTLEMENT_MIN_POINTS,
                "reason": "no_reserved_charges",
            }
        if not force and total < LEGACY_SERVICE_BATCH_SETTLEMENT_MIN_POINTS:
            return {
                "created": False,
                "status": "reserved",
                "reserved_total_points": total,
                "threshold_points": LEGACY_SERVICE_BATCH_SETTLEMENT_MIN_POINTS,
                "reason": "below_threshold",
            }
        batch_uuid = str(uuid.uuid4())
        charge_uuids = [row["charge_uuid"] for row in rows]
        metadata = {
            "settlement_layer": "service_fee_subledger",
            "settlement_policy": "legacy_service_fee_batch_debit",
            "batch_uuid": batch_uuid,
            "batch_reason": str(reason or "threshold")[:80],
            "charge_count": len(rows),
            "charge_uuid_hash": sha256_text(canonical_json(charge_uuids)),
            "charge_uuids_sample": charge_uuids[:20],
            "source_wallet_address": source_address,
            "chain_branch": active_branch,
            "chain_fee_policy": "batched_l1_debit_no_per_service_fee",
            "batch_threshold_points": LEGACY_SERVICE_BATCH_SETTLEMENT_MIN_POINTS,
        }
        unfreeze_row, _unfreeze_created = self._record_transaction(
            conn,
            user_id=user_id,
            currency_type=DISPLAY_CURRENCY,
            direction="unfreeze",
            amount=total,
            action_type="service_fee_batch_unfreeze",
            reference_type="service_fee_batch",
            reference_id=batch_uuid,
            idempotency_key=f"service_fee_batch:{batch_uuid}:unfreeze",
            reason="service fee batch settlement unfreeze",
            public_metadata=metadata,
            actor=actor,
        )
        debit_row, _debit_created = self._record_transaction(
            conn,
            user_id=user_id,
            currency_type=DISPLAY_CURRENCY,
            direction="debit",
            amount=total,
            action_type="service_fee_batch_debit",
            reference_type="service_fee_batch",
            reference_id=batch_uuid,
            idempotency_key=f"service_fee_batch:{batch_uuid}:debit",
            reason="service fee batch settlement debit",
            public_metadata=metadata,
            actor=actor,
        )
        settled_at = utc_now()
        conn.execute(
            f"""
            UPDATE points_service_fee_charges
            SET status='settled', batch_uuid=?, unfreeze_ledger_uuid=?, debit_ledger_uuid=?, settled_at=?
            WHERE id IN ({", ".join("?" for _ in rows)}) AND status='reserved'
            """,
            (
                batch_uuid,
                unfreeze_row["ledger_uuid"],
                debit_row["ledger_uuid"],
                settled_at,
                *[int(row["id"]) for row in rows],
            ),
        )
        return {
            "created": True,
            "status": "settled",
            "batch_uuid": batch_uuid,
            "settled_amount_points": total,
            "reserved_total_points": 0,
            "threshold_points": LEGACY_SERVICE_BATCH_SETTLEMENT_MIN_POINTS,
            "charge_count": len(rows),
            "charge_uuid_hash": metadata["charge_uuid_hash"],
            "unfreeze_ledger": self.serialize_ledger(unfreeze_row),
            "debit_ledger": self.serialize_ledger(debit_row),
        }

    def spend_points(self, *, user_id, item_key, quantity=1, reference_type=None, reference_id=None, idempotency_key=None, metadata=None, actor=None, override_amount=None, source_wallet_address=None, request_uuid=None, signature=None, chain_enabled=True):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            self._assert_chain_writable(conn, "spend points")
            active_branch = self._canonical_branch_uuid(conn)
            self._assert_canonical_write_branch(conn, active_branch)
            item = conn.execute("SELECT * FROM economy_price_catalog WHERE item_key=? AND enabled=1", (item_key,)).fetchone()
            if not item:
                raise ValueError("price catalog item not found or disabled")
            quantity = max(1, int(quantity or 1))
            if override_amount is not None:
                amount = int(override_amount)
            else:
                amount = int(item["base_price"]) * quantity
            if amount <= 0:
                raise ValueError("amount must be positive")
            metadata = dict(metadata or {})
            source_address = str(source_wallet_address or metadata.get("source_wallet_address") or "").strip().lower()
            source_wallet = None
            if chain_enabled:
                if source_address:
                    source_wallet = self._wallet_identity_row_for_user_address(conn, user_id, source_address, active_only=True)
                    if not source_wallet:
                        raise ValueError("source wallet does not belong to current user")
                    self._assert_wallet_identity_can_spend(source_wallet, context="service fee")
                    available = self._wallet_identity_available_for_address(conn, user_id=int(user_id), address=source_address)
                    if available < amount:
                        raise ValueError("insufficient balance")
                    metadata["source_wallet_address"] = source_address
                else:
                    primary_address = self._primary_wallet_address_for_read(conn, user_id)
                    if primary_address:
                        source_address = str(primary_address or "").strip().lower()
                        primary_wallet = self._wallet_identity_row_for_user_address(conn, user_id, source_address, active_only=True)
                        if primary_wallet:
                            self._assert_wallet_identity_can_spend(primary_wallet, context="service fee")
                        metadata["source_wallet_address"] = source_address
            else:
                source_address = ""
                metadata.pop("source_wallet_address", None)
                metadata["points_chain_enabled"] = False
            spend_bucket = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
            effective_idempotency_key = idempotency_key or f"spend:{user_id}:{item_key}:{quantity}:{spend_bucket}"
            existing_charge = self._service_fee_charge_by_idempotency(conn, effective_idempotency_key)
            if existing_charge:
                if str(existing_charge["chain_branch"] if "chain_branch" in existing_charge.keys() else self._main_branch_uuid()) != active_branch:
                    raise ValueError("service fee idempotency key belongs to a non-canonical branch")
                conn.commit()
                return self._service_fee_existing_response(conn, charge_row=existing_charge, item=item)
            charge_uuid = str(request_uuid or uuid.uuid4()).strip()[:120]
            if not charge_uuid:
                raise ValueError("request_uuid is required")
            existing_uuid = self._service_fee_charge_by_uuid(conn, charge_uuid)
            if existing_uuid:
                if str(existing_uuid["chain_branch"] if "chain_branch" in existing_uuid.keys() else self._main_branch_uuid()) != active_branch:
                    raise ValueError("service fee request_uuid belongs to a non-canonical branch")
                if existing_uuid["idempotency_key"] == effective_idempotency_key:
                    conn.commit()
                    return self._service_fee_existing_response(conn, charge_row=existing_uuid, item=item)
                raise ValueError("service fee request_uuid conflict")
            reference_type = reference_type or "price_catalog"
            reference_id = reference_id or item_key
            if chain_enabled and not is_pc0_internal_address(source_address):
                raise ValueError(
                    "cold wallet direct service payment is disabled in the pc0 model; "
                    "deposit to the pc0 internal custody wallet first, or use a future cold-chain approval payment rail"
                )
            if chain_enabled:
                self._verify_service_fee_wallet_signature(
                    conn=conn,
                    user_id=user_id,
                    source_wallet_address=source_address,
                    item_key=item_key,
                    quantity=quantity,
                    amount_points=amount,
                    request_uuid=charge_uuid,
                    reference_type=reference_type,
                    reference_id=reference_id,
                    signature=signature,
                    chain_branch=active_branch,
                )
            if chain_enabled and is_pc0_internal_address(source_address):
                public_metadata = {
                    "item_key": item_key,
                    "quantity": quantity,
                    "settlement_layer": "internal_hot_wallet_ledger",
                    "settlement_policy": "internal_hot_wallet_immediate_debit",
                    "chain_fee_policy": "pc0_internal_service_payment_no_network_fee",
                    "settlement_rail": "internal_hot_wallet",
                    "chain_required": False,
                    "approval_required": False,
                    "network_fee_points": 0,
                    "service_fee_points": amount,
                    "source_wallet_address": source_address,
                    "destination_fund_key": SERVICE_FEE_REVENUE_DESTINATION_FUND,
                    "destination_wallet_address": economy_fund_address(self.chain_secret, SERVICE_FEE_REVENUE_DESTINATION_FUND),
                    "service_fee_charge_uuid": charge_uuid,
                    **metadata,
                }
                row, created = self._record_transaction(
                    conn,
                    user_id=user_id,
                    currency_type=item["currency_type"],
                    direction="debit",
                    amount=amount,
                    action_type=f"service_fee_internal_debit:{item_key}",
                    reference_type=reference_type,
                    reference_id=reference_id,
                    idempotency_key=f"service_fee_internal_debit:{charge_uuid}",
                    reason=f"internal pc0 service fee:{item['item_name']}",
                    public_metadata=public_metadata,
                    actor=actor,
                )
                now = utc_now()
                conn.execute(
                    """
                    INSERT INTO points_service_fee_charges (
                        charge_uuid, chain_branch, user_id, item_key, quantity, amount_points, currency_type,
                        source_wallet_address, status, idempotency_key, freeze_ledger_uuid, unfreeze_ledger_uuid,
                        debit_ledger_uuid, batch_uuid, reference_type, reference_id, metadata_json, created_at, settled_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'settled', ?, '', '', ?, '', ?, ?, ?, ?, ?)
                    """,
                    (
                        charge_uuid,
                        active_branch,
                        int(user_id),
                        item_key,
                        quantity,
                        amount,
                        normalize_currency_type(item["currency_type"]),
                        source_address,
                        effective_idempotency_key,
                        row["ledger_uuid"],
                        reference_type,
                        str(reference_id or ""),
                        _metadata_json_checked(metadata, label="service fee metadata"),
                        now,
                        now,
                    ),
                )
                charge_row = self._service_fee_charge_by_uuid(conn, charge_uuid)
                conn.commit()
                return {
                    "ok": True,
                    "created": created,
                    "settlement_layer": "internal_hot_wallet_ledger",
                    "settlement_policy": "internal_hot_wallet_immediate_debit",
                    "charge": self._serialize_service_fee_charge(charge_row),
                    "ledger": self._serialize_ledger_for_read(conn, row),
                    "settlement": {
                        "created": True,
                        "status": "settled",
                        "settled_amount_points": amount,
                        "reason": "pc0_internal_immediate",
                        "debit_ledger": self._serialize_ledger_for_read(conn, row),
                    },
                    "wallet": self.get_wallet(user_id),
                    "item": dict(item),
                }
            public_metadata = {
                "item_key": item_key,
                "quantity": quantity,
                "settlement_layer": "basic_points_ledger",
                "settlement_policy": "basic_points_immediate_debit",
                "chain_fee_policy": "points_chain_disabled_no_chain_fee",
                "settlement_rail": "basic_points",
                "chain_required": False,
                "approval_required": False,
                "network_fee_points": 0,
                "service_fee_points": amount,
                "service_fee_charge_uuid": charge_uuid,
                "destination_fund_key": SERVICE_FEE_REVENUE_DESTINATION_FUND,
                **metadata,
            }
            row, created = self._record_transaction(
                conn,
                user_id=user_id,
                currency_type=item["currency_type"],
                direction="debit",
                amount=amount,
                action_type=f"service_fee_internal_debit:{item_key}",
                reference_type=reference_type,
                reference_id=reference_id,
                idempotency_key=f"service_fee_basic_debit:{charge_uuid}",
                reason=f"basic points service fee:{item['item_name']}",
                public_metadata=public_metadata,
                actor=actor,
            )
            now = utc_now()
            conn.execute(
                """
                INSERT INTO points_service_fee_charges (
                    charge_uuid, chain_branch, user_id, item_key, quantity, amount_points, currency_type,
                    source_wallet_address, status, idempotency_key, freeze_ledger_uuid, unfreeze_ledger_uuid,
                    debit_ledger_uuid, batch_uuid, reference_type, reference_id, metadata_json, created_at, settled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'settled', ?, '', '', ?, '', ?, ?, ?, ?, ?)
                """,
                (
                    charge_uuid,
                    active_branch,
                    int(user_id),
                    item_key,
                    quantity,
                    amount,
                    normalize_currency_type(item["currency_type"]),
                    source_address,
                    effective_idempotency_key,
                    row["ledger_uuid"],
                    reference_type,
                    str(reference_id or ""),
                    _metadata_json_checked(metadata, label="service fee metadata"),
                    now,
                    now,
                ),
            )
            charge_row = self._service_fee_charge_by_uuid(conn, charge_uuid)
            conn.commit()
            return {
                "ok": True,
                "created": created,
                "settlement_layer": "basic_points_ledger",
                "settlement_policy": "basic_points_immediate_debit",
                "charge": self._serialize_service_fee_charge(charge_row),
                "ledger": self._serialize_ledger_for_read(conn, row),
                "settlement": {
                    "created": True,
                    "status": "settled",
                    "settled_amount_points": amount,
                    "reason": "basic_points_immediate",
                    "debit_ledger": self._serialize_ledger_for_read(conn, row),
                },
                "wallet": self.get_wallet(user_id),
                "item": dict(item),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def pay_violation_fine(self, *, user_id, fine_uuid, amount_points, source_wallet_address=None, request_uuid=None, signature=None, actor=None, metadata=None):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            self._assert_chain_writable(conn, "pay violation fine")
            active_branch = self._canonical_branch_uuid(conn)
            self._assert_canonical_write_branch(conn, active_branch)
            amount = int(amount_points or 0)
            if amount <= 0:
                raise ValueError("amount must be positive")
            fine_ref = str(fine_uuid or "").strip()
            if not fine_ref:
                raise ValueError("fine_uuid required")
            source_address = str(source_wallet_address or "").strip().lower()
            if source_address:
                source_wallet = self._wallet_identity_row_for_user_address(conn, user_id, source_address, active_only=True)
                if not source_wallet:
                    raise ValueError("source wallet does not belong to current user")
            else:
                source_address = self._primary_wallet_address_for_read(conn, user_id) or ""
                source_wallet = self._wallet_identity_row_for_user_address(conn, user_id, source_address, active_only=True) if source_address else None
            if source_wallet:
                self._assert_wallet_identity_can_spend(source_wallet, context="violation fine payment")
                available = self._wallet_identity_available_for_address(conn, user_id=int(user_id), address=source_address)
                if available < amount:
                    raise ValueError("insufficient balance")
            charge_uuid = str(request_uuid or uuid.uuid4()).strip()[:120]
            if not charge_uuid:
                raise ValueError("request_uuid is required")
            self._verify_service_fee_wallet_signature(
                conn=conn,
                user_id=user_id,
                source_wallet_address=source_address,
                item_key="violation_fine",
                quantity=1,
                amount_points=amount,
                request_uuid=charge_uuid,
                reference_type="violation_fine",
                reference_id=fine_ref,
                signature=signature,
                chain_branch=active_branch,
            )
            public_metadata = {
                "item_key": "violation_fine",
                "fine_uuid": fine_ref,
                "source_wallet_address": source_address,
                "destination_fund_key": "burn",
                "destination_policy": "fine_payment_burn",
                "chain_branch": active_branch,
                **dict(metadata or {}),
            }
            row, created = self._record_transaction(
                conn,
                user_id=user_id,
                currency_type=DISPLAY_CURRENCY,
                direction="debit",
                amount=amount,
                action_type="spend:violation_fine",
                reference_type="violation_fine",
                reference_id=fine_ref,
                idempotency_key=f"violation_fine:{fine_ref}:pay",
                reason="violation fine payment to burn",
                public_metadata=public_metadata,
                actor=actor,
            )
            conn.commit()
            return {
                "ok": True,
                "created": created,
                "ledger": self.serialize_ledger(row, include_user_id=True),
                "wallet": self.get_wallet(user_id),
                "charge_uuid": charge_uuid,
                "destination_fund_key": "burn",
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def admin_adjust(self, *, actor, user_id, currency_type, direction, amount, reason, reference_id=None, idempotency_key=None):
        raise PermissionError("blockchain_permission_model: manual points adjustment is disabled; use official_wallet_grant to a public wallet address")

    def _official_wallet_grant_locked(self, conn, *, actor, destination_wallet_address, amount, reason="", request_uuid=None):
        amount = int(amount or 0)
        if amount <= 0:
            raise ValueError("amount must be positive")
        destination = str(destination_wallet_address or "").strip().lower()
        if not destination:
            raise ValueError("destination wallet address required")
        destination_fund_key = self._official_transfer_destination_fund_key(destination)
        if not destination_fund_key and not WALLET_ADDRESS_RE.fullmatch(destination):
            raise ValueError("destination wallet address format is invalid")
        reason_text = public_currency_text(str(reason or "official wallet grant").strip() or "official wallet grant")[:240]
        request_uuid = str(request_uuid or uuid.uuid4()).strip()[:120]
        if not request_uuid:
            raise ValueError("request_uuid required")
        self._assert_chain_writable(conn, "official wallet grant")
        sender_user_id = int(actor_value(actor, "id") or 0)
        if sender_user_id <= 0:
            root_row = conn.execute("SELECT id FROM users WHERE username='root' LIMIT 1").fetchone()
            sender_user_id = int(root_row["id"] or 0) if root_row else 0
        if sender_user_id <= 0:
            raise PermissionError("root actor id required")
        destination_wallet = None if destination_fund_key else self._wallet_identity_owner_for_address(conn, destination)
        destination_unowned = 0
        if destination_fund_key:
            recipient_user_id = sender_user_id
            transaction_type = "official_fund_transfer"
        elif destination_wallet and destination_wallet["user_id"]:
            recipient_user_id = int(destination_wallet["user_id"])
            transaction_type = "official_wallet_grant"
        else:
            recipient_user_id = sender_user_id
            transaction_type = "official_wallet_grant"
            destination_unowned = 1
        if is_pc0_internal_address(destination) and not destination_fund_key and not destination_wallet:
            raise ValueError("pc0 internal destination must be a registered official hot wallet or system fund")
        source_address = economy_fund_address(self.chain_secret, "official_treasury")
        active_branch = self._canonical_branch_uuid(conn)
        self._assert_canonical_write_branch(conn, active_branch)
        internal_official_transfer = bool(
            destination_fund_key
            or (destination_wallet and is_pc0_internal_address(destination))
        )
        settlement_rail = (
            "internal_system_burn"
            if destination_fund_key == "burn"
            else "internal_hot_wallet"
            if internal_official_transfer
            else "cold_chain"
        )
        chain_required = 0 if internal_official_transfer else 1
        approval_required = 0 if internal_official_transfer else 1
        payload = {
            "chain_branch": active_branch,
            "source_wallet_address": source_address,
            "destination_wallet_address": destination,
            "amount_points": amount,
            "fee_points": 0,
            "memo": reason_text,
            "transaction_type": transaction_type,
            "source_fund_key": "official_treasury",
        }
        request_hash = self._transfer_request_payload_hash(payload)
        tx_group_hash = sha256_text(f"points-chain-{transaction_type}:{active_branch}:{request_uuid}:{request_hash}")
        existing = conn.execute(
            "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
            (request_uuid,),
        ).fetchone()
        if existing:
            if str(existing["chain_branch"] if "chain_branch" in existing.keys() else "main") != active_branch:
                raise ValueError("transaction idempotency key belongs to a non-canonical branch")
            if existing["request_hash"] != request_hash:
                raise ValueError("transaction idempotency key conflict")
            existing = self._maybe_finalize_transfer_request_locked(conn, existing, actor=actor)
            return {
                "ok": True,
                "created": False,
                "tx_group_hash": existing["tx_group_hash"],
                "transaction_hash": existing["tx_group_hash"],
                "transaction": self._transfer_request_public_payload(conn, existing),
                "wallet": None if self._explorer_fund_key_for_address(existing["destination_wallet_address"]) or self._transfer_request_destination_unowned(existing) else self.wallet_payload_for_read(conn, int(existing["recipient_user_id"])),
                "destination_fund_key": self._explorer_fund_key_for_address(existing["destination_wallet_address"]) or "",
                "destination_unowned": self._transfer_request_destination_unowned(existing),
                **self._transfer_request_ledgers(conn, request_uuid),
            }
        conn.execute(
            """
            INSERT INTO points_chain_transfer_requests (
                request_uuid, chain_branch, request_hash, tx_group_hash, sender_user_id, recipient_user_id,
                source_wallet_address, destination_wallet_address, destination_unowned, amount_points, fee_points,
                transaction_type, source_fund_key, memo,
                settlement_rail, chain_required, approval_required, network_fee_points, service_fee_points,
                transfer_out_ledger_uuid, transfer_in_ledger_uuid, fee_ledger_uuid, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'official_treasury', ?, ?, ?, ?, 0, 0, NULL, NULL, NULL, 'pending', ?)
            """,
            (
                request_uuid,
                active_branch,
                request_hash,
                tx_group_hash,
                sender_user_id,
                recipient_user_id,
                source_address,
                destination,
                destination_unowned,
                amount,
                transaction_type,
                reason_text,
                settlement_rail,
                chain_required,
                approval_required,
                utc_now(),
            ),
        )
        req = conn.execute(
            "SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?",
            (request_uuid,),
        ).fetchone()
        if internal_official_transfer:
            finalized = self._maybe_finalize_transfer_request_locked(conn, req, actor=actor)
            return {
                "ok": True,
                "created": True,
                "warnings": [],
                "notifications": {"completed": {"event": "completed", "internal": True, "all_sent": True}},
                "tx_group_hash": tx_group_hash,
                "transaction_hash": tx_group_hash,
                "transaction": self._transfer_request_public_payload(conn, finalized),
                "wallet": None if destination_fund_key or destination_unowned else self.wallet_payload_for_read(conn, int(recipient_user_id)),
                "destination_fund_key": destination_fund_key or "",
                "destination_unowned": bool(destination_unowned),
                **self._transfer_request_ledgers(conn, request_uuid),
            }
        notification_status = self._notify_wallet_transfer_pending(conn, req)
        warnings = [] if notification_status.get("all_sent") else ["notification_delivery_failed"]
        return {
            "ok": True,
            "created": True,
            "warnings": warnings,
            "notifications": {"pending": notification_status},
            "tx_group_hash": tx_group_hash,
            "transaction_hash": tx_group_hash,
            "transaction": self._transfer_request_public_payload(conn, req),
            "wallet": None if destination_fund_key or destination_unowned else self.wallet_payload_for_read(conn, int(recipient_user_id)),
            "destination_fund_key": destination_fund_key or "",
            "destination_unowned": bool(destination_unowned),
            **self._transfer_request_ledgers(conn, request_uuid),
        }

    def official_wallet_grant(self, *, actor, destination_wallet_address, amount, reason="", request_uuid=None, governance_proposal_uuid=None):
        if not governance_proposal_uuid:
            raise PermissionError("official treasury transfer requires executed governance proposal")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            proposal = self._refresh_governance_proposal_locked(conn, governance_proposal_uuid)
            if not proposal or proposal["lifecycle_status"] != "EXECUTED":
                raise PermissionError("official treasury transfer requires executed governance proposal")
            if str(proposal["action_type"] or "").strip().upper() not in {"TREASURY_TRANSFER", "EXCHANGE_FUND_REPLENISH", "CONTEST_REWARD_PAYOUT"}:
                raise PermissionError("governance proposal is not an official treasury transfer")
            if self._governance_execution_payload_hash_for_row(proposal) != proposal["execution_payload_hash"]:
                raise ValueError("governance execution payload hash mismatch")
            if str(destination_wallet_address or "").strip().lower() != str(proposal["target_wallet_address"] or "").strip().lower():
                raise PermissionError("official treasury transfer payload does not match governance proposal destination")
            if int(amount or 0) != int(proposal["requested_amount"] or 0):
                raise PermissionError("official treasury transfer payload does not match governance proposal amount")
            result = self._official_wallet_grant_locked(
                conn,
                actor=actor,
                destination_wallet_address=destination_wallet_address,
                amount=amount,
                reason=reason,
                request_uuid=request_uuid,
            )
            conn.commit()
            return result
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _create_pending_reward(self, conn, *, user_id, currency_type, amount, action_type, reference_type=None, reference_id=None, metadata=None, submitted_by=None):
        currency_type = normalize_currency_type(currency_type)
        cur = conn.execute(
            """
            INSERT INTO points_pending_rewards (
                user_id, currency_type, amount, action_type, reference_type, reference_id,
                status, submitted_by, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
            """,
            (int(user_id), currency_type, int(amount), action_type, reference_type, str(reference_id or ""), submitted_by, _json_dumps(metadata or {}), utc_now()),
        )
        return conn.execute("SELECT * FROM points_pending_rewards WHERE id=?", (cur.lastrowid,)).fetchone()

    def create_pending_reward(self, *, actor, user_id, currency_type, amount, action_type, reference_type=None, reference_id=None, metadata=None):
        raise PermissionError("blockchain_permission_model: pending rewards are disabled; use official wallet or governance-approved on-chain rules")

    def review_pending_reward(self, *, actor, pending_reward_id, decision, review_note=""):
        raise PermissionError("blockchain_permission_model: pending reward review is disabled")

    def compensate_ledger(self, *, actor, ledger_uuid, reason):
        reason = str(reason or "").strip()
        if not reason:
            raise ValueError("reason is required")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            self._assert_chain_writable(conn, "ledger compensation")
            original = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (str(ledger_uuid or ""),)).fetchone()
            if not original:
                raise ValueError("ledger not found")
            if original["ledger_hash"] != compute_ledger_hash(original):
                raise ValueError("ledger is tampered; repair or restore it before compensation")
            if str(original["action_type"] or "").startswith(("compensation:", "rollback:")):
                raise ValueError("compensation ledger cannot be compensated again")
            reverse_map = {
                "credit": "reverse",
                "transfer_in": "transfer_out",
                "debit": "credit",
                "transfer_out": "transfer_in",
                "freeze": "unfreeze",
                "unfreeze": "freeze",
            }
            reverse_direction = reverse_map.get(original["direction"])
            if not reverse_direction:
                raise ValueError("unsupported compensation direction")
            compensation_row, created = self._record_transaction(
                conn,
                user_id=original["user_id"],
                currency_type=original["currency_type"],
                direction=reverse_direction,
                amount=original["amount"],
                action_type=f"compensation:{original['action_type']}",
                reference_type="ledger_compensation",
                reference_id=original["ledger_uuid"],
                idempotency_key=f"compensation:{original['ledger_uuid']}",
                reason=reason,
                public_metadata={
                    "compensation_of": original["ledger_uuid"],
                    "original_direction": original["direction"],
                    "original_action_type": original["action_type"],
                },
                actor=actor,
                risk_flag="compensation",
                risk_score=20,
            )
            self._audit_log(
                conn,
                "LEDGER_COMPENSATION",
                "warning",
                f"compensate ledger {original['ledger_uuid']}",
                actor=actor,
                target_user_id=original["user_id"],
                ledger_id=compensation_row["id"],
                metadata={"original_ledger_uuid": original["ledger_uuid"], "reason": reason, "created": created},
            )
            conn.commit()
            return {
                "ok": True,
                "created": created,
                "original_ledger": self._explorer_public_ledger(conn, original),
                "compensation_ledger": self._explorer_public_ledger(conn, compensation_row),
                "wallet": self.get_wallet(original["user_id"]),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def rollback_ledger(self, *, actor, ledger_uuid, reason):
        raise PermissionError("blockchain_permission_model: rollback is disabled; append a compensation transaction instead")

    def list_ledger(self, *, user_id=None, limit=50, offset=0, include_user_id=False):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            limit = min(200, max(1, int(limit or 50)))
            offset = max(0, int(offset or 0))
            if user_id:
                rows = conn.execute(
                    "SELECT * FROM points_ledger WHERE user_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                    (int(user_id), limit, offset),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM points_ledger ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
            return [self._serialize_ledger_for_read(conn, row, include_user_id=include_user_id) for row in rows]
        finally:
            conn.close()

    def list_rules(self):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            rows = conn.execute("SELECT * FROM points_rules ORDER BY rule_key").fetchall()
            return [{**dict(row), "currency_type": DISPLAY_CURRENCY} for row in rows]
        finally:
            conn.close()

    def list_catalog(self, *, include_disabled=False, category=None):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            clauses = []
            params = []
            if not include_disabled:
                clauses.append("enabled=1")
            if category:
                clauses.append("category=?")
                params.append(str(category))
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM economy_price_catalog{where} ORDER BY category, enabled DESC, base_price, item_key",
                tuple(params),
            ).fetchall()
            return [{**dict(row), "currency_type": DISPLAY_CURRENCY, "metadata": _json_loads(row["metadata_json"], {})} for row in rows]
        finally:
            conn.close()

    def upsert_catalog_item(
        self,
        *,
        actor,
        item_key,
        item_name,
        category,
        base_price,
        dynamic_pricing=0,
        min_price=None,
        max_price=None,
        enabled=True,
        metadata=None,
    ):
        key = str(item_key or "").strip().lower()
        if not PRICE_ITEM_KEY_RE.fullmatch(key):
            raise ValueError("item_key must be 2-80 chars: lowercase letters, numbers, _, :, -")
        item_name = str(item_name or "").strip()
        if not item_name:
            raise ValueError("item_name required")
        category = str(category or "").strip().lower()
        if not PRICE_CATEGORY_RE.fullmatch(category):
            raise ValueError("category must be 2-80 chars: lowercase letters, numbers, _, :, -")
        try:
            base_price = int(base_price)
        except Exception as exc:
            raise ValueError("base_price must be an integer") from exc
        if base_price < 1 or base_price > 1_000_000_000:
            raise ValueError("base_price must be 1-1000000000")
        dynamic_pricing = 1 if dynamic_pricing else 0
        min_price = None if min_price in (None, "") else int(min_price)
        max_price = None if max_price in (None, "") else int(max_price)
        if min_price is not None and min_price < 1:
            raise ValueError("min_price must be positive")
        if max_price is not None and max_price < 1:
            raise ValueError("max_price must be positive")
        if min_price is not None and max_price is not None and min_price > max_price:
            raise ValueError("min_price cannot exceed max_price")
        if min_price is not None and base_price < min_price:
            raise ValueError("base_price cannot be lower than min_price")
        if max_price is not None and base_price > max_price:
            raise ValueError("base_price cannot exceed max_price")

        metadata = metadata if isinstance(metadata, dict) else {}
        cleaned_metadata = {}
        for meta_key, meta_value in metadata.items():
            if isinstance(meta_key, str) and len(meta_key) <= 80:
                cleaned_metadata[meta_key] = meta_value
        if category == "cloud_drive":
            try:
                storage_bytes = int(cleaned_metadata.get("storage_bytes") or 0)
                duration_days = int(cleaned_metadata.get("duration_days") or 0)
            except Exception as exc:
                raise ValueError("cloud_drive metadata requires integer storage_bytes and duration_days") from exc
            if storage_bytes < 1:
                raise ValueError("cloud_drive storage_bytes must be positive")
            if duration_days < 1 or duration_days > 3650:
                raise ValueError("cloud_drive duration_days must be 1-3650")
            cleaned_metadata["storage_bytes"] = storage_bytes
            cleaned_metadata["duration_days"] = duration_days
            cleaned_metadata["label"] = str(cleaned_metadata.get("label") or item_name).strip()[:120]

        now = utc_now()
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.execute(
                """
                INSERT INTO economy_price_catalog (
                    item_key, item_name, category, currency_type, base_price,
                    dynamic_pricing, min_price, max_price, enabled, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    item_name=excluded.item_name,
                    category=excluded.category,
                    currency_type=excluded.currency_type,
                    base_price=excluded.base_price,
                    dynamic_pricing=excluded.dynamic_pricing,
                    min_price=excluded.min_price,
                    max_price=excluded.max_price,
                    enabled=excluded.enabled,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    item_name,
                    category,
                    INTERNAL_CURRENCY,
                    base_price,
                    dynamic_pricing,
                    min_price,
                    max_price,
                    1 if enabled else 0,
                    _json_dumps(cleaned_metadata),
                    now,
                    now,
                ),
            )
            self._audit_log(
                conn,
                "price_catalog_upsert",
                "info",
                f"updated price catalog item {key}",
                actor=actor,
                metadata={
                    "item_key": key,
                    "item_name": item_name,
                    "category": category,
                    "base_price": base_price,
                    "enabled": bool(enabled),
                },
            )
            conn.commit()
            row = conn.execute("SELECT * FROM economy_price_catalog WHERE item_key=?", (key,)).fetchone()
            return {**dict(row), "currency_type": DISPLAY_CURRENCY, "metadata": _json_loads(row["metadata_json"], {})}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list_pending_rewards(self, *, status="pending", limit=100):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM points_pending_rewards WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status or "pending", min(200, max(1, int(limit or 100)))),
            ).fetchall()
            return [{**dict(row), "currency_type": DISPLAY_CURRENCY} for row in rows]
        finally:
            conn.close()

    def list_admin_adjustments(self, *, limit=100):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT l.*, target.username AS target_username, actor.username AS actor_username
                FROM points_ledger l
                LEFT JOIN users target ON target.id=l.user_id
                LEFT JOIN users actor ON actor.id=l.created_by
                WHERE l.action_type LIKE 'admin_adjust_%'
                   OR l.action_type LIKE 'rollback:%'
                   OR l.action_type IN (
                       'admin_initial_grant',
                       'admin_weekly_salary',
                       'birthday_gift',
                       'game_daily_challenge_reward',
                       'game_weekly_leaderboard_reward',
                       'trading_bot_weekly_competition_reward',
                       'new_user_signup_bonus',
                       'user_initial_grant'
                   )
                ORDER BY l.id DESC LIMIT ?
                """,
                (min(200, max(1, int(limit or 100))),),
            ).fetchall()
            adjustments = []
            for row in rows:
                item = self.serialize_ledger(row, include_user_id=True)
                item["target_username"] = row["target_username"] or f"user:{row['user_id']}"
                item["actor_username"] = row["actor_username"] or (f"user:{row['created_by']}" if row["created_by"] else "system")
                item["signed_amount"] = int(row["amount"]) if row["direction"] in {"credit", "transfer_in"} else -int(row["amount"])
                adjustments.append(item)
            return adjustments
        finally:
            conn.close()

    def seal_block(self, *, actor=None, limit=100):
        # Phase 7 mode guard. Runs BEFORE we open a write transaction
        # so disallowed modes never even reach the chain INSERT path.
        self._assert_production_mode("seal_block")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            self._assert_chain_writable(conn, "block seal")
            branch = self._canonical_branch_uuid(conn)
            rows = self._pc1_unsealed_ledger_rows_locked(
                conn,
                branch=branch,
                limit=min(500, max(1, int(limit or 100))),
            )
            if not rows:
                conn.commit()
                counts = self._pc1_chain_ledger_count_snapshot_locked(conn, branch=branch)
                return {
                    "ok": True,
                    "sealed": False,
                    "msg": "沒有可封存的 PC1 canonical chain ledger；pc0 站內帳本保留在 internal audit rail",
                    "counts": counts,
                }
            last = conn.execute("SELECT * FROM points_chain_blocks ORDER BY block_number DESC LIMIT 1").fetchone()
            block_number = int(last["block_number"] + 1) if last else 1
            prev_hash = last["block_hash"] if last else None
            hashes = [row["ledger_hash"] for row in rows]
            sealed_at = utc_now()
            block_data = {
                "block_number": block_number,
                "previous_block_hash": prev_hash,
                "merkle_root": merkle_root(hashes),
                "ledger_count": len(rows),
                "first_ledger_id": rows[0]["id"],
                "last_ledger_id": rows[-1]["id"],
                "sealed_at": sealed_at,
            }
            block_hash = compute_block_hash(block_data)
            cur = conn.execute(
                """
                INSERT INTO points_chain_blocks (
                    block_number, previous_block_hash, merkle_root, block_hash,
                    ledger_count, first_ledger_id, last_ledger_id, sealed_by,
                    sealed_by_node, sealed_at, seal_status, anchor_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sealed', 'local_only', ?)
                """,
                (
                    block_number,
                    prev_hash,
                    block_data["merkle_root"],
                    block_hash,
                    len(rows),
                    rows[0]["id"],
                    rows[-1]["id"],
                    actor_value(actor, "id"),
                    "single-node",
                    sealed_at,
                    sealed_at,
                ),
            )
            block_id = cur.lastrowid
            ids = [row["id"] for row in rows]
            conn.execute(
                f"UPDATE points_ledger SET chain_block_id=? WHERE id IN ({','.join('?' for _ in ids)})",
                (block_id, *ids),
            )
            self._audit_log(conn, "POINTS_BLOCK_SEALED", "info", f"sealed points block {block_number}", actor=actor, block_id=block_id, metadata={"ledger_count": len(rows), "chain_branch": branch})
            block = conn.execute("SELECT * FROM points_chain_blocks WHERE id=?", (block_id,)).fetchone()
            self._ensure_local_node(conn)
            conn.execute(
                """
                INSERT OR REPLACE INTO points_chain_block_signatures (
                    block_id, node_id, signature_algorithm, public_key_fingerprint, signature, signed_at
                ) VALUES (?, 'single-node', 'hmac-sha256', ?, ?, ?)
                """,
                (block_id, self._node_fingerprint(), self._sign_block(block), sealed_at),
            )
            conn.commit()
            return {
                "ok": True,
                "sealed": True,
                "block": dict(block),
                "backup": None,
                "backup_policy": "disabled_append_only_chain",
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def seal_due_block(self, *, actor=None, ledger_threshold=DEFAULT_BLOCK_LEDGER_THRESHOLD, max_interval_seconds=DEFAULT_BLOCK_MAX_INTERVAL_SECONDS, limit=100):
        schedule = self.block_schedule(
            ledger_threshold=ledger_threshold,
            max_interval_seconds=max_interval_seconds,
            verify=False,
        )
        if not schedule.get("due"):
            return {"ok": True, "sealed": False, "msg": schedule.get("message") or "not due", "schedule": schedule}
        verification = self.verify_chain_bounded_snapshot()
        if verification.get("ok") is not True:
            return {
                "ok": False,
                "sealed": False,
                "msg": "PointsChain bounded 驗證失敗",
                "schedule": self.block_schedule(
                    ledger_threshold=ledger_threshold,
                    max_interval_seconds=max_interval_seconds,
                    verification=verification,
                ),
                "verification": verification,
            }
        schedule = self.block_schedule(
            ledger_threshold=ledger_threshold,
            max_interval_seconds=max_interval_seconds,
            verification=verification,
        )
        if not schedule.get("due"):
            return {"ok": True, "sealed": False, "msg": schedule.get("message") or "not due", "schedule": schedule}
        result = self.seal_block(actor=actor, limit=limit)
        result["schedule"] = schedule
        return result

    def force_seal_block(self, *, actor=None, reason="", limit=500):
        # Phase 7: chain writes require production or dev_ready. When mode-
        # switch / snapshot / restore flows call us from disallowed modes
        # (e.g. switching to internal_test, where the chain SHOULD NOT
        # be touched), we degrade gracefully to a no-op rather than
        # raising — the violation event is still recorded by the guard.
        try:
            self._assert_production_mode("force_seal_block")
        except ChainModeViolation as exc:
            return {
                "ok": True,
                "sealed": False,
                "skipped": True,
                "reason": str(reason or ""),
                "msg": f"skipped: chain writes require production/dev_ready (mode={exc.mode!r})",
            }
        verification = self.verify_chain_bounded_snapshot()
        if verification.get("ok") is not True:
            return {"ok": False, "sealed": False, "msg": "PointsChain bounded 驗證失敗", "verification": verification}
        result = self.seal_block(actor=actor, limit=limit)
        result["forced"] = True
        result["reason"] = str(reason or "")
        result["pre_seal_verification"] = {
            "bounded": bool(verification.get("bounded")),
            "verification_mode": verification.get("verification_mode"),
            "recent_checked": verification.get("recent_checked"),
            "counts": verification.get("counts") or {},
        }
        return result

    def _verify_chain_on_conn(self, conn, *, mark_safe_mode=True):
        self.ensure_schema(conn)
        self._ensure_local_node(conn)
        errors = []
        previous_by_branch = {}
        previous_ledger_by_branch = {}
        for row in conn.execute("SELECT * FROM points_ledger ORDER BY id ASC").fetchall():
                branch = str(row["chain_branch"] if "chain_branch" in row.keys() else self._main_branch_uuid())
                previous = previous_by_branch.get(branch)
                previous_ledger = previous_ledger_by_branch.get(branch)
                if row["previous_ledger_hash"] != previous:
                    errors.append({
                        "type": "ledger_previous_hash",
                        "severity": "critical",
                        "message": f"ledger #{row['id']} previous hash mismatch on branch {branch}",
                        "chain_branch": branch,
                        "ledger_id": row["id"],
                        "ledger_uuid": row["ledger_uuid"],
                        "expected_previous_ledger_hash": previous,
                        "actual_previous_ledger_hash": row["previous_ledger_hash"],
                        "previous_ledger_id": previous_ledger["id"] if previous_ledger else None,
                        "previous_ledger_uuid": previous_ledger["ledger_uuid"] if previous_ledger else None,
                        "ledger": self.serialize_ledger(row, include_user_id=True),
                    })
                expected = compute_ledger_hash(row)
                if row["ledger_hash"] != expected:
                    errors.append({
                        "type": "ledger_hash",
                        "severity": "critical",
                        "message": f"ledger #{row['id']} content hash mismatch",
                        "ledger_id": row["id"],
                        "ledger_uuid": row["ledger_uuid"],
                        "expected_ledger_hash": expected,
                        "actual_ledger_hash": row["ledger_hash"],
                        "ledger": self.serialize_ledger(row, include_user_id=True),
                    })
                previous_by_branch[branch] = row["ledger_hash"]
                previous_ledger_by_branch[branch] = row
        previous_block = None
        for block in conn.execute("SELECT * FROM points_chain_blocks ORDER BY block_number ASC").fetchall():
                if block["previous_block_hash"] != previous_block:
                    errors.append({
                        "type": "block_previous_hash",
                        "severity": "critical",
                        "message": f"block #{block['block_number']} previous hash mismatch",
                        "block_id": block["id"],
                        "block_number": block["block_number"],
                        "expected_previous_block_hash": previous_block,
                        "actual_previous_block_hash": block["previous_block_hash"],
                    })
                ledgers = conn.execute(
                    "SELECT * FROM points_ledger WHERE chain_block_id=? ORDER BY id ASC",
                    (block["id"],),
                ).fetchall()
                hashes = [row["ledger_hash"] for row in ledgers]
                if len(hashes) != int(block["ledger_count"]):
                    errors.append({
                        "type": "block_ledger_count",
                        "severity": "critical",
                        "message": f"block #{block['block_number']} ledger count mismatch",
                        "block_id": block["id"],
                        "block_number": block["block_number"],
                        "expected_ledger_count": int(block["ledger_count"]),
                        "actual_ledger_count": len(hashes),
                        "first_ledger_id": block["first_ledger_id"],
                        "last_ledger_id": block["last_ledger_id"],
                    })
                noncanonical = [
                    ledger for ledger in ledgers
                    if not self._ledger_is_pc1_canonical_sealable(conn, ledger)
                ]
                if noncanonical:
                    errors.append({
                        "type": "block_contains_noncanonical_ledger",
                        "severity": "critical",
                        "message": f"block #{block['block_number']} contains pc0/internal operational ledger entries",
                        "block_id": block["id"],
                        "block_number": block["block_number"],
                        "ledger_uuids": [ledger["ledger_uuid"] for ledger in noncanonical[:25]],
                    })
                expected_merkle_root = merkle_root(hashes)
                if expected_merkle_root != block["merkle_root"]:
                    errors.append({
                        "type": "block_merkle_root",
                        "severity": "critical",
                        "message": f"block #{block['block_number']} merkle root mismatch",
                        "block_id": block["id"],
                        "block_number": block["block_number"],
                        "expected_merkle_root": expected_merkle_root,
                        "actual_merkle_root": block["merkle_root"],
                        "first_ledger_id": block["first_ledger_id"],
                        "last_ledger_id": block["last_ledger_id"],
                        "ledger_uuids": [ledger["ledger_uuid"] for ledger in ledgers],
                    })
                expected_block_hash = compute_block_hash(block)
                if expected_block_hash != block["block_hash"]:
                    errors.append({
                        "type": "block_hash",
                        "severity": "critical",
                        "message": f"block #{block['block_number']} block hash mismatch",
                        "block_id": block["id"],
                        "block_number": block["block_number"],
                        "expected_block_hash": expected_block_hash,
                        "actual_block_hash": block["block_hash"],
                    })
                signature = conn.execute(
                    """
                    SELECT * FROM points_chain_block_signatures
                    WHERE block_id=? AND node_id='single-node'
                    """,
                    (block["id"],),
                ).fetchone()
                if not signature:
                    errors.append({
                        "type": "block_signature_missing",
                        "severity": "high",
                        "message": f"block #{block['block_number']} signature missing",
                        "block_id": block["id"],
                        "block_number": block["block_number"],
                    })
                elif signature["signature_algorithm"] != "hmac-sha256" or signature["public_key_fingerprint"] != self._node_fingerprint() or signature["signature"] != self._sign_block(block):
                    errors.append({
                        "type": "block_signature_invalid",
                        "severity": "critical",
                        "message": f"block #{block['block_number']} signature invalid",
                        "block_id": block["id"],
                        "block_number": block["block_number"],
                        "signature_algorithm": signature["signature_algorithm"],
                        "public_key_fingerprint": signature["public_key_fingerprint"],
                        "expected_public_key_fingerprint": self._node_fingerprint(),
                    })
                previous_block = block["block_hash"]
        schema_errors = self._schema_integrity_errors_locked(conn)
        errors.extend(schema_errors)
        repairs = []
        if not errors:
            wallet_errors = self._verify_wallets_against_ledger(conn)
            if wallet_errors and all(err.get("repairable_derived_cache") for err in wallet_errors):
                rebuild = self._rebuild_wallets_from_ledger(conn)
                wallet_errors_after_rebuild = self._verify_wallets_against_ledger(conn)
                if wallet_errors_after_rebuild:
                    errors.extend(wallet_errors_after_rebuild)
                else:
                    repairs.append({
                        "type": "wallet_identity_cache_rebuilt",
                        "wallet_errors_repaired": len(wallet_errors),
                        "wallet_rebuild": rebuild,
                    })
                    self._audit_log(
                        conn,
                        "POINTS_CHAIN_WALLET_CACHE_REBUILT",
                        "info",
                        "Wallet aggregate cache rebuilt from wallet identity ledger replay",
                        metadata={"wallet_errors": wallet_errors, "wallet_rebuild": rebuild},
                    )
            else:
                errors.extend(wallet_errors)
        if not errors:
            identity_balance_verify = self.verify_wallet_identity_balances(conn, chain_branch=self._canonical_branch_uuid(conn), mode="full")
            if not identity_balance_verify.get("ok"):
                rebuild = self.rebuild_wallet_identity_balances(conn, self._canonical_branch_uuid(conn))
                identity_balance_verify = self.verify_wallet_identity_balances(conn, chain_branch=self._canonical_branch_uuid(conn), mode="full")
                if identity_balance_verify.get("ok"):
                    repairs.append({
                        "type": "wallet_identity_balances_rebuilt",
                        "wallet_balance_rebuild": rebuild,
                    })
                else:
                    errors.extend(identity_balance_verify.get("errors") or [])
        branch = self._canonical_branch_uuid(conn)
        pc1_counts = self._pc1_chain_ledger_count_snapshot_locked(conn, branch=branch)
        counts = {
            "ledger_entries": conn.execute("SELECT COUNT(*) AS c FROM points_ledger").fetchone()["c"],
            "sealed_blocks": conn.execute("SELECT COUNT(*) AS c FROM points_chain_blocks").fetchone()["c"],
            "unsealed_entries": pc1_counts["pc1_unsealed_entries"],
            "pc1_canonical_entries": pc1_counts["pc1_canonical_entries"],
            "pc1_unsealed_entries": pc1_counts["pc1_unsealed_entries"],
            "pc0_operational_entries": pc1_counts["pc0_operational_entries"],
            "pc0_operational_unsealed_entries": pc1_counts["pc0_operational_unsealed_entries"],
            "audit_events": conn.execute("SELECT COUNT(*) AS c FROM points_chain_audit_logs").fetchone()["c"],
            "wallets": conn.execute("SELECT COUNT(*) AS c FROM points_wallets").fetchone()["c"],
        }
        result = {"ok": not errors, "errors": errors[:100], "error_count": len(errors), "counts": counts}
        if repairs:
            result["repairs"] = repairs
        if mark_safe_mode and errors:
            result["safe_mode"] = self._enter_safe_mode(conn, result, "chain_verification_failed")
        elif mark_safe_mode:
            state = self._safe_mode_status(conn)
            if state.get("safe_mode"):
                now = utc_now()
                plan = state.get("restore_plan") if isinstance(state.get("restore_plan"), dict) else {}
                conn.execute(
                    """
                    UPDATE points_chain_recovery_state
                    SET safe_mode=0, restored_at=COALESCE(restored_at, ?), updated_at=?, restore_plan_json=?
                    WHERE id=1
                    """,
                    (
                        now,
                        now,
                        _json_dumps({
                            **plan,
                            "auto_cleared_after_clean_verify": True,
                            "verification_ok": True,
                            "verification_counts": counts,
                            "cleared_at": now,
                        }),
                    ),
                )
                self._audit_log(
                    conn,
                    "POINTS_CHAIN_SAFE_MODE_CLEARED",
                    "info",
                    "PointsChain safe mode cleared after clean verification",
                    metadata={"verification": result},
                )
                result["safe_mode"] = self._safe_mode_status(conn)
        return result

    def _schema_integrity_errors_locked(self, conn):
        errors = []
        transfer_cols = set(table_columns(conn, "points_chain_transfer_requests") or [])
        if transfer_cols:
            if "settlement_rail" in transfer_cols:
                placeholders = ",".join("?" for _ in TRANSFER_SETTLEMENT_RAILS)
                rows = conn.execute(
                    f"""
                    SELECT id, request_uuid, settlement_rail
                    FROM points_chain_transfer_requests
                    WHERE settlement_rail IS NULL
                       OR settlement_rail=''
                       OR settlement_rail NOT IN ({placeholders})
                    ORDER BY id ASC
                    LIMIT 25
                    """,
                    tuple(sorted(TRANSFER_SETTLEMENT_RAILS)),
                ).fetchall()
                for row in rows:
                    errors.append({
                        "type": "schema_invalid_settlement_rail",
                        "severity": "critical",
                        "message": "points_chain_transfer_requests has invalid settlement_rail",
                        "table": "points_chain_transfer_requests",
                        "row_id": row["id"],
                        "request_uuid": row["request_uuid"],
                        "settlement_rail": row["settlement_rail"],
                    })
            bool_columns = [column for column in ("chain_required", "approval_required") if column in transfer_cols]
            if bool_columns:
                where = " OR ".join(f"{column} NOT IN (0,1)" for column in bool_columns)
                rows = conn.execute(
                    f"""
                    SELECT id, request_uuid, chain_required, approval_required
                    FROM points_chain_transfer_requests
                    WHERE {where}
                    ORDER BY id ASC
                    LIMIT 25
                    """
                ).fetchall()
                for row in rows:
                    invalid_columns = [
                        column
                        for column in bool_columns
                        if int(row[column] if row[column] is not None else -1) not in {0, 1}
                    ]
                    errors.append({
                        "type": "schema_invalid_bool",
                        "severity": "critical",
                        "message": "points_chain_transfer_requests has non-boolean chain/approval flag",
                        "table": "points_chain_transfer_requests",
                        "row_id": row["id"],
                        "request_uuid": row["request_uuid"],
                        "invalid_columns": invalid_columns,
                    })
            fee_columns = [column for column in ("network_fee_points", "service_fee_points") if column in transfer_cols]
            if fee_columns:
                where = " OR ".join(f"{column}<0" for column in fee_columns)
                rows = conn.execute(
                    f"""
                    SELECT id, request_uuid, network_fee_points, service_fee_points
                    FROM points_chain_transfer_requests
                    WHERE {where}
                    ORDER BY id ASC
                    LIMIT 25
                    """
                ).fetchall()
                for row in rows:
                    invalid_columns = [
                        column
                        for column in fee_columns
                        if int(row[column] if row[column] is not None else 0) < 0
                    ]
                    errors.append({
                        "type": "schema_negative_fee",
                        "severity": "critical",
                        "message": "points_chain_transfer_requests has negative fee column",
                        "table": "points_chain_transfer_requests",
                        "row_id": row["id"],
                        "request_uuid": row["request_uuid"],
                        "invalid_columns": invalid_columns,
                    })
        bridge_cols = set(table_columns(conn, "points_chain_bridge_events") or [])
        if bridge_cols and "bridge_uuid" in bridge_cols:
            rows = conn.execute(
                """
                SELECT id, chain_tx_hash, status, bridge_uuid
                FROM points_chain_bridge_events
                WHERE bridge_uuid IS NULL OR bridge_uuid=''
                ORDER BY id ASC
                LIMIT 25
                """
            ).fetchall()
            for row in rows:
                errors.append({
                    "type": "schema_empty_bridge_uuid",
                    "severity": "critical",
                    "message": "points_chain_bridge_events has empty bridge_uuid",
                    "table": "points_chain_bridge_events",
                    "row_id": row["id"],
                    "chain_tx_hash": row["chain_tx_hash"],
                    "status": row["status"],
                })
        return errors

    def schema_integrity_report(self):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            errors = self._schema_integrity_errors_locked(conn)
            return {"ok": not errors, "errors": errors[:100], "error_count": len(errors)}
        finally:
            conn.close()

    def _pc0_liability_merkle_locked(self, conn, *, branch_uuid=None):
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        if not table_columns(conn, "points_wallet_identities"):
            return {
                "leaf_count": 0,
                "member_leaf_count": 0,
                "root_leaf_count": 0,
                "available_points": 0,
                "frozen_points": 0,
                "pending_outgoing_points": 0,
                "total_points": 0,
                "merkle_root": merkle_root([]),
                "sample_leaves": [],
            }
        rows = conn.execute(
            """
            SELECT w.user_id, LOWER(w.address) AS address, COALESCE(LOWER(u.username), '') AS username
            FROM points_wallet_identities w
            LEFT JOIN users u ON u.id=w.user_id
            WHERE w.status='active'
              AND w.wallet_type='official_hot'
              AND LOWER(w.address) LIKE 'pc0%'
            ORDER BY w.user_id ASC, w.id ASC
            """
        ).fetchall()
        leaves = []
        hashes = []
        totals = {
            "leaf_count": 0,
            "member_leaf_count": 0,
            "root_leaf_count": 0,
            "available_points": 0,
            "frozen_points": 0,
            "pending_outgoing_points": 0,
            "total_points": 0,
        }
        for row in rows:
            user_id = int(row["user_id"] or 0)
            address = str(row["address"] or "").strip().lower()
            if not user_id or not is_pc0_internal_address(address):
                continue
            state = self._wallet_identity_balances_for_user(conn, user_id, branch_uuid=branch)
            balance = (state.get("balances") or {}).get(address) or {}
            available = int(balance.get("balance") or 0)
            frozen = int(balance.get("frozen") or 0)
            pending = int(balance.get("pending_outgoing") or 0)
            payload = {
                "version": "pc0_liability_leaf_v1",
                "user_id": user_id,
                "username": str(row["username"] or ""),
                "address": address,
                "available_points": available,
                "frozen_points": frozen,
                "pending_outgoing_points": pending,
                "total_points": available + frozen,
                "chain_branch": branch,
            }
            leaf_hash = sha256_text(canonical_json(payload))
            hashes.append(leaf_hash)
            leaves.append({
                "version": payload["version"],
                "address": address,
                "available_points": available,
                "frozen_points": frozen,
                "pending_outgoing_points": pending,
                "total_points": available + frozen,
                "chain_branch": branch,
                "account_ref_hash": sha256_text(canonical_json({
                    "user_id": user_id,
                    "username": str(row["username"] or ""),
                    "address": address,
                })),
                "leaf_hash": leaf_hash,
            })
            totals["leaf_count"] += 1
            if str(row["username"] or "") == "root":
                totals["root_leaf_count"] += 1
            else:
                totals["member_leaf_count"] += 1
            totals["available_points"] += available
            totals["frozen_points"] += frozen
            totals["pending_outgoing_points"] += pending
            totals["total_points"] += available + frozen
        return {
            **totals,
            "merkle_root": merkle_root(hashes),
            "sample_leaves": leaves[:20],
        }

    def _bridge_settlement_integrity_locked(self, conn):
        errors = []
        if not table_columns(conn, "points_chain_bridge_events"):
            return {"ok": True, "errors": [], "error_count": 0, "counts": {}}
        counts = {
            "credited_deposits": 0,
            "pending_deposits": 0,
            "failed_deposits": 0,
            "orphan_credited_deposits": 0,
            "premature_credited_deposits": 0,
            "credited_deposits_to_pc0_destination": 0,
            "orphan_internal_deposit_credits": 0,
        }
        for row in conn.execute("SELECT * FROM points_chain_bridge_events WHERE bridge_type='deposit' ORDER BY id ASC").fetchall():
            status = str(row["status"] or "").strip().lower()
            if status == "credited":
                counts["credited_deposits"] += 1
            elif status == "pending":
                counts["pending_deposits"] += 1
            elif status:
                counts["failed_deposits"] += 1
            if status != "credited":
                continue
            if is_pc0_internal_address(row["destination_address"]):
                counts["credited_deposits_to_pc0_destination"] += 1
                errors.append({
                    "type": "bridge_deposit_destination_pc0",
                    "severity": "critical",
                    "message": "credited deposit bridge event uses pc0 as chain-side destination",
                    "bridge_uuid": row["bridge_uuid"],
                    "chain_tx_hash": row["chain_tx_hash"],
                })
            if str(row["risk_status"] or "").strip().lower() != "accepted" or int(row["confirmations"] or 0) < int(row["required_confirmations"] or 20):
                counts["premature_credited_deposits"] += 1
                errors.append({
                    "type": "bridge_deposit_premature_credit",
                    "severity": "critical",
                    "message": "deposit bridge credited before accepted risk and required confirmations",
                    "bridge_uuid": row["bridge_uuid"],
                    "chain_tx_hash": row["chain_tx_hash"],
                    "confirmations": int(row["confirmations"] or 0),
                    "required_confirmations": int(row["required_confirmations"] or 20),
                    "risk_status": row["risk_status"],
                })
            ledger_uuid = str(row["internal_ledger_uuid"] or "").strip()
            ledger = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (ledger_uuid,)).fetchone() if ledger_uuid else None
            if not ledger:
                counts["orphan_credited_deposits"] += 1
                errors.append({
                    "type": "bridge_deposit_missing_internal_credit",
                    "severity": "critical",
                    "message": "credited deposit bridge event has no matching internal ledger credit",
                    "bridge_uuid": row["bridge_uuid"],
                    "chain_tx_hash": row["chain_tx_hash"],
                    "internal_ledger_uuid": ledger_uuid,
                })
        rows = conn.execute(
            """
            SELECT l.ledger_uuid, l.reference_id
            FROM points_ledger l
            WHERE l.status='confirmed'
              AND l.action_type='deposit_credit'
              AND l.reference_type='deposit_bridge'
              AND NOT EXISTS (
                SELECT 1
                FROM points_chain_bridge_events b
                WHERE b.bridge_type='deposit'
                  AND b.status='credited'
                  AND b.internal_ledger_uuid=l.ledger_uuid
              )
            ORDER BY l.id ASC
            LIMIT 25
            """
        ).fetchall()
        for row in rows:
            counts["orphan_internal_deposit_credits"] += 1
            errors.append({
                "type": "bridge_internal_credit_missing_event",
                "severity": "critical",
                "message": "deposit internal credit has no credited bridge event",
                "ledger_uuid": row["ledger_uuid"],
                "reference_id": row["reference_id"],
            })
        return {"ok": not errors, "errors": errors[:100], "error_count": len(errors), "counts": counts}

    def _pc0_pc1_boundary_integrity_locked(self, conn):
        errors = []
        counts = {
            "sealed_non_canonical_ledgers": 0,
            "sealed_pc0_operational_ledgers": 0,
        }
        rows = conn.execute(
            """
            SELECT *
            FROM points_ledger
            WHERE status='confirmed'
              AND chain_block_id IS NOT NULL
            ORDER BY id ASC
            """
        ).fetchall()
        for row in rows:
            if self._ledger_is_pc1_canonical_sealable(conn, row):
                continue
            raw_public_metadata = row["public_metadata_json"] if "public_metadata_json" in row.keys() else None
            public_metadata = _json_loads(raw_public_metadata, {})
            flow = self._ledger_wallet_flow_for_read(conn, row)
            settlement_rail = self._ledger_settlement_rail_for_read(
                conn,
                row,
                flow=flow,
                public_metadata=public_metadata,
            )
            counts["sealed_non_canonical_ledgers"] += 1
            if settlement_rail in PC0_OPERATIONAL_SETTLEMENT_RAILS:
                counts["sealed_pc0_operational_ledgers"] += 1
            errors.append({
                "type": "pc0_operational_ledger_sealed_into_pc1_block",
                "severity": "critical",
                "message": "non-canonical or pc0 operational ledger row is sealed into a pc1 canonical block",
                "ledger_uuid": row["ledger_uuid"],
                "transaction_hash": row["transaction_hash"] if "transaction_hash" in row.keys() else row["ledger_hash"],
                "action_type": row["action_type"],
                "settlement_rail": settlement_rail or "unknown",
                "chain_block_id": int(row["chain_block_id"] or 0),
            })
        return {"ok": not errors, "errors": errors[:100], "error_count": len(errors), "counts": counts}

    def _financial_invariant_report_locked(self, conn, *, economy_layer=None):
        self.ensure_schema(conn)
        branch = self._canonical_branch_uuid(conn)
        if economy_layer is None:
            circulation = self._official_hot_wallet_circulation_with_economy_breakdown_locked(
                conn,
                branch_uuid=branch,
                actor={"role": "system", "id": None},
            )
            bootstrap_economy_layer(
                conn,
                chain_secret=self.chain_secret,
                actor={"role": "system", "id": None},
                chain_branch=branch,
            )
            replay = replay_economy_events(
                conn,
                chain_secret=self.chain_secret,
                persist_cache=False,
                chain_branch=branch,
            )
            supply_equation = economy_supply_equation_report(replay=replay, circulation=circulation)
            economy_layer = {
                "funds": replay.get("balances") or {},
                "supply": {
                    "max_supply": replay.get("max_supply"),
                    "minted_total": replay.get("minted_total"),
                    "burned_total": replay.get("burned_total"),
                    "active_supply": replay.get("active_supply"),
                    "mint_remaining": replay.get("mint_remaining"),
                },
                "supply_equation": supply_equation,
            }
        supply = economy_layer.get("supply") if isinstance(economy_layer, dict) else {}
        bridge = economy_layer.get("supply_equation") if isinstance(economy_layer, dict) else {}
        supply = supply if isinstance(supply, dict) else {}
        bridge = bridge if isinstance(bridge, dict) else {}
        flow = bridge.get("bridge_flow_totals") if isinstance(bridge.get("bridge_flow_totals"), dict) else {}
        liability_merkle = self._pc0_liability_merkle_locked(conn, branch_uuid=branch)
        official_treasury = int(bridge.get("official_treasury_balance") or 0)
        promo_fund = int(bridge.get("promo_fund_balance") or 0)
        exchange_fund = int(bridge.get("exchange_fund_balance") or 0)
        platform_funds = official_treasury + promo_fund + exchange_fund
        member_liability = int(bridge.get("member_internal_circulating_points") or 0)
        root_liability = int(bridge.get("root_internal_circulating_points") or 0)
        wrapped_operational_liabilities = member_liability + root_liability + platform_funds
        off_wallet = int(bridge.get("off_wallet_economy_external_points") or 0)
        active_supply = int(supply.get("active_supply") or 0)
        max_supply = int(supply.get("max_supply") or bridge.get("max_supply") or 0)
        burned = int(supply.get("burned_total") or bridge.get("burned_total") or 0)
        mint_remaining = int(supply.get("mint_remaining") or bridge.get("mint_remaining") or 0)
        bridge_integrity = self._bridge_settlement_integrity_locked(conn)
        ledger_boundary_integrity = self._pc0_pc1_boundary_integrity_locked(conn)
        invariants = [
            {
                "name": "wrapped_supply_within_canonical_locked_reserve",
                "pass": wrapped_operational_liabilities <= active_supply,
                "lhs_points": wrapped_operational_liabilities,
                "rhs_points": active_supply,
            },
            {
                "name": "finalized_supply_equation_balanced",
                "pass": int(bridge.get("bridged_supply_equation_gap_points") or 0) == 0,
                "gap_points": int(bridge.get("bridged_supply_equation_gap_points") or 0),
            },
            {
                "name": "wallet_ledger_matches_economy_events",
                "pass": int(bridge.get("ledger_vs_economy_external_gap_points") or 0) == 0,
                "gap_points": int(bridge.get("ledger_vs_economy_external_gap_points") or 0),
            },
            {
                "name": "bridge_flow_reconstructs_external_supply",
                "pass": int(flow.get("economy_flow_reconciliation_gap_points") or 0) == 0,
                "gap_points": int(flow.get("economy_flow_reconciliation_gap_points") or 0),
            },
            {
                "name": "no_direct_cold_to_pc0_transfer_requests",
                "pass": int(flow.get("invalid_direct_cold_to_pc0_request_points") or 0) == 0,
                "invalid_points": int(flow.get("invalid_direct_cold_to_pc0_request_points") or 0),
            },
            {
                "name": "reserve_and_balances_never_negative",
                "pass": min(active_supply, off_wallet, wrapped_operational_liabilities, official_treasury, promo_fund, exchange_fund, burned, mint_remaining) >= 0,
            },
            {
                "name": "bridge_settlement_integrity",
                "pass": bridge_integrity.get("ok") is True,
                "error_count": int(bridge_integrity.get("error_count") or 0),
            },
            {
                "name": "pc0_operational_ledgers_not_sealed_into_pc1_blocks",
                "pass": ledger_boundary_integrity.get("ok") is True,
                "error_count": int(ledger_boundary_integrity.get("error_count") or 0),
                "sealed_pc0_operational_ledgers": int((ledger_boundary_integrity.get("counts") or {}).get("sealed_pc0_operational_ledgers") or 0),
            },
        ]
        errors = []
        for invariant in invariants:
            if invariant.get("pass") is True:
                continue
            errors.append({
                "type": "financial_invariant_failed",
                "severity": "critical",
                "message": invariant["name"],
                "invariant": invariant,
            })
        errors.extend(bridge_integrity.get("errors") or [])
        errors.extend(ledger_boundary_integrity.get("errors") or [])
        ok = not errors
        return {
            "ok": ok,
            "status": "pass" if ok else "fail",
            "model": "pc1_canonical_reserve_pc0_wrapped_operational_v1",
            "chain_branch": branch,
            "canonical_reserve": {
                "source_of_truth": "points_economy_events",
                "max_supply_points": max_supply,
                "canonical_locked_reserve_points": active_supply,
                "active_supply_points": active_supply,
                "burned_points": burned,
                "mint_remaining_points": mint_remaining,
                "off_wallet_external_points": off_wallet,
            },
            "wrapped_operational_liabilities": {
                "wrapped_supply_points": wrapped_operational_liabilities,
                "finalized_total_points": wrapped_operational_liabilities,
                "member_internal_points": member_liability,
                "root_internal_points": root_liability,
                "official_treasury_points": official_treasury,
                "promo_fund_points": promo_fund,
                "exchange_fund_points": exchange_fund,
                "liability_merkle": liability_merkle,
            },
            "pending_settlement": {
                "hot_to_cold_pending_points": int(flow.get("hot_to_cold_pending_points") or 0),
                "deposit_pending_points": int(flow.get("deposit_pending_points") or 0),
                "hot_to_cold_network_fee_points": int(flow.get("hot_to_cold_network_fee_points") or 0),
                "deposit_network_fee_points": int(flow.get("deposit_network_fee_points") or 0),
            },
            "bridge_reconstruction": {
                "pc0_out_confirmed_points": int(flow.get("hot_to_cold_confirmed_points") or 0),
                "deposit_credited_points": int(flow.get("deposit_credited_points") or 0),
                "current_cold_chain_or_bridge_external_points": int(flow.get("current_cold_chain_or_bridge_external_points") or off_wallet),
                "flow_gap_points": int(flow.get("economy_flow_reconciliation_gap_points") or 0),
                "bridge_integrity": bridge_integrity,
            },
            "ledger_boundary": ledger_boundary_integrity,
            "invariants": invariants,
            "errors": errors[:100],
            "error_count": len(errors),
        }

    def financial_invariant_report(self):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            report = self._financial_invariant_report_locked(conn)
            conn.commit()
            return report
        finally:
            conn.close()

    def operations_control_snapshot(self, *, recent_limit=50):
        """Bounded management-plane snapshot for economy and exchange ops.

        This deliberately avoids full ledger replay. It gives operators the
        current control-plane posture for initial grants, service-fee linkage,
        chain queues, emergency governance, and exchange fund watermarks.
        """
        started = _monotonic_time.perf_counter()
        recent_limit = max(10, min(200, int(recent_limit or 50)))
        phases = []
        phase_started = started

        def mark_phase(name):
            nonlocal phase_started
            now = _monotonic_time.perf_counter()
            phases.append({"phase": name, "elapsed_ms": round((now - phase_started) * 1000, 3)})
            phase_started = now

        def int_value(value):
            try:
                return int(value or 0)
            except Exception:
                return 0

        def first_int(row):
            if not row:
                return 0
            try:
                return int_value(row[0])
            except Exception:
                keys = row.keys()
                return int_value(row[keys[0]]) if keys else 0

        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            ensure_economy_layer_schema(conn)
            try:
                self._ensure_transaction_dispute_schema(conn)
                dispute_schema_error = ""
            except Exception as exc:
                dispute_schema_error = str(exc)[:240]
            branch = self._canonical_branch_uuid(conn)
            mark_phase("schema")

            user_cols = table_columns(conn, "users")
            has_users = {"id", "username", "role"}.issubset(user_cols)
            status_filter = "AND COALESCE(status, 'active')='active'" if "status" in user_cols else ""
            if has_users:
                eligible_admin_sql = f"""
                    FROM users u
                    WHERE COALESCE(u.username, '')<>'root'
                      AND u.role IN ('manager', 'super_admin')
                      {status_filter}
                """
                eligible_user_sql = f"""
                    FROM users u
                    WHERE COALESCE(u.username, '')<>'root'
                      AND u.role NOT IN ('manager', 'super_admin')
                      {status_filter}
                """
                admin_eligible = first_int(conn.execute(f"SELECT COUNT(*) {eligible_admin_sql}").fetchone())
                user_eligible = first_int(conn.execute(f"SELECT COUNT(*) {eligible_user_sql}").fetchone())
                admin_missing = first_int(conn.execute(
                    f"""
                    SELECT COUNT(*) {eligible_admin_sql}
                      AND NOT EXISTS (
                        SELECT 1 FROM points_ledger l
                        WHERE l.chain_branch=? AND l.user_id=u.id AND l.action_type='admin_initial_grant'
                      )
                    """,
                    (branch,),
                ).fetchone())
                user_missing = first_int(conn.execute(
                    f"""
                    SELECT COUNT(*) {eligible_user_sql}
                      AND NOT EXISTS (
                        SELECT 1 FROM points_ledger l
                        WHERE l.chain_branch=? AND l.user_id=u.id AND l.action_type='user_initial_grant'
                      )
                    """,
                    (branch,),
                ).fetchone())
                signup_missing = first_int(conn.execute(
                    f"""
                    SELECT COUNT(*) {eligible_user_sql}
                      AND NOT EXISTS (
                        SELECT 1 FROM points_ledger l
                        WHERE l.chain_branch=? AND l.user_id=u.id AND l.action_type='new_user_signup_bonus'
                      )
                    """,
                    (branch,),
                ).fetchone())
            else:
                admin_eligible = user_eligible = admin_missing = user_missing = signup_missing = 0
            grant_rows = conn.execute(
                """
                SELECT action_type,
                       COUNT(*) AS ledger_entries,
                       COUNT(DISTINCT user_id) AS recipients,
                       COALESCE(SUM(amount), 0) AS amount_points,
                       MAX(created_at) AS latest_at
                FROM points_ledger
                WHERE chain_branch=?
                  AND action_type IN (
                    'admin_initial_grant',
                    'user_initial_grant',
                    'new_user_signup_bonus',
                    'birthday_gift',
                    'admin_weekly_salary'
                  )
                GROUP BY action_type
                """,
                (branch,),
            ).fetchall()
            grant_summary = {
                row["action_type"]: {
                    "ledger_entries": int_value(row["ledger_entries"]),
                    "recipients": int_value(row["recipients"]),
                    "amount_points": int_value(row["amount_points"]),
                    "latest_at": row["latest_at"],
                }
                for row in grant_rows
            }
            mark_phase("initial_distribution")

            service_fee_rows = conn.execute(
                """
                SELECT status,
                       COUNT(*) AS charge_count,
                       COALESCE(SUM(amount_points), 0) AS amount_points,
                       MAX(created_at) AS latest_at
                FROM points_service_fee_charges
                WHERE chain_branch=?
                GROUP BY status
                """,
                (branch,),
            ).fetchall()
            service_fee_by_status = {
                row["status"]: {
                    "charge_count": int_value(row["charge_count"]),
                    "amount_points": int_value(row["amount_points"]),
                    "latest_at": row["latest_at"],
                }
                for row in service_fee_rows
            }
            catalog_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END), 0) AS enabled_count,
                    COALESCE(SUM(CASE WHEN enabled=0 THEN 1 ELSE 0 END), 0) AS disabled_count
                FROM economy_price_catalog
                """
            ).fetchone()
            service_spend_row = conn.execute(
                """
                SELECT COUNT(*) AS ledger_entries,
                       COALESCE(SUM(amount), 0) AS amount_points,
                       MAX(created_at) AS latest_at
                FROM points_ledger
                WHERE chain_branch=?
                  AND (
                    action_type LIKE 'spend:%'
                    OR action_type IN ('service_fee_debit', 'service_fee_settlement')
                    OR reference_type='price_catalog'
                  )
                """,
                (branch,),
            ).fetchone()
            mark_phase("service_fee_connection")

            chain_counts = conn.execute(
                """
                SELECT
                    COUNT(*) AS ledger_entries,
                    COALESCE(SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END), 0) AS confirmed_entries,
                    COALESCE(SUM(CASE WHEN status='confirmed' AND chain_block_id IS NULL THEN 1 ELSE 0 END), 0) AS unsealed_entries,
                    COALESCE(SUM(CASE WHEN status!='confirmed' THEN 1 ELSE 0 END), 0) AS nonconfirmed_entries,
                    MAX(id) AS latest_ledger_id,
                    MAX(created_at) AS latest_ledger_at
                FROM points_ledger
                WHERE chain_branch=?
                """,
                (branch,),
            ).fetchone()
            transfer_rows = conn.execute(
                """
                SELECT status,
                       COUNT(*) AS request_count,
                       COALESCE(SUM(amount_points), 0) AS amount_points,
                       MAX(created_at) AS latest_at
                FROM points_chain_transfer_requests
                WHERE chain_branch=?
                GROUP BY status
                """,
                (branch,),
            ).fetchall()
            transfer_by_status = {
                row["status"]: {
                    "request_count": int_value(row["request_count"]),
                    "amount_points": int_value(row["amount_points"]),
                    "latest_at": row["latest_at"],
                }
                for row in transfer_rows
            }
            blocks = conn.execute(
                """
                SELECT COUNT(*) AS block_count,
                       MAX(block_number) AS latest_block_number,
                       MAX(sealed_at) AS latest_sealed_at
                FROM points_chain_blocks
                """
            ).fetchone()
            recent_unsealed = [
                {
                    "ledger_uuid": row["ledger_uuid"],
                    "transaction_hash": row["transaction_hash"] if "transaction_hash" in row.keys() else row["ledger_hash"],
                    "action_type": row["action_type"],
                    "amount": int_value(row["amount"]),
                    "created_at": row["created_at"],
                }
                for row in conn.execute(
                    """
                    SELECT *
                    FROM points_ledger
                    WHERE chain_branch=? AND status='confirmed' AND chain_block_id IS NULL
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (branch, min(recent_limit, 50)),
                ).fetchall()
            ]
            mark_phase("private_chain")

            policy_row = conn.execute("SELECT * FROM points_economy_policy WHERE id=1").fetchone()
            policy_payload = _json_loads(policy_row["policy_json"], {}) if policy_row else {}
            economy_snapshot = conn.execute(
                "SELECT * FROM points_economy_snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            derived_rows = conn.execute(
                """
                SELECT fund_key, address, balance, replay_height, replay_event_hash, updated_at
                FROM points_economy_derived_balances
                ORDER BY fund_key
                """
            ).fetchall()
            fund_balances = {
                row["fund_key"]: {
                    "address": row["address"],
                    "balance_points": int_value(row["balance"]),
                    "replay_height": int_value(row["replay_height"]),
                    "updated_at": row["updated_at"],
                }
                for row in derived_rows
            }
            incident_rows = conn.execute(
                """
                SELECT status, severity, COUNT(*) AS incident_count
                FROM points_economy_incidents
                GROUP BY status, severity
                """
            ).fetchall()
            incidents = [
                {
                    "status": row["status"],
                    "severity": row["severity"],
                    "incident_count": int_value(row["incident_count"]),
                }
                for row in incident_rows
            ]
            mark_phase("economy_model")

            governance_rows = conn.execute(
                """
                SELECT lifecycle_status, proposal_severity, COUNT(*) AS proposal_count
                FROM points_chain_governance_proposals
                GROUP BY lifecycle_status, proposal_severity
                """
            ).fetchall()
            governance_by_status = {}
            for row in governance_rows:
                lifecycle = str(row["lifecycle_status"] or "unknown")
                severity = str(row["proposal_severity"] or "NORMAL")
                governance_by_status.setdefault(lifecycle, {})
                governance_by_status[lifecycle][severity] = int_value(row["proposal_count"])
            emergency_proposals = first_int(conn.execute(
                """
                SELECT COUNT(*)
                FROM points_chain_governance_proposals
                WHERE proposal_severity IN ('HIGH', 'CRITICAL')
                   OR governance_domain='EMERGENCY_SECURITY'
                   OR action_type IN ('EMERGENCY_LOCKDOWN', 'ROLLBACK_BRANCH')
                """
            ).fetchone())
            dispute_counts = {}
            if not dispute_schema_error:
                dispute_rows = conn.execute(
                    """
                    SELECT status, COUNT(*) AS dispute_count
                    FROM points_chain_transaction_disputes
                    GROUP BY status
                    """
                ).fetchall()
                dispute_counts = {row["status"]: int_value(row["dispute_count"]) for row in dispute_rows}
            active_freezes = first_int(conn.execute(
                "SELECT COUNT(*) FROM points_chain_address_freezes WHERE status='active'"
            ).fetchone())
            active_provisional_freezes = first_int(conn.execute(
                "SELECT COUNT(*) FROM points_chain_address_provisional_freezes WHERE status='active'"
            ).fetchone())
            safe_mode = self._safe_mode_status(conn)
            mark_phase("emergency")

            wallet_identity_counts = {}
            if table_columns(conn, "points_wallet_identities"):
                wallet_identity_rows = conn.execute(
                    """
                    SELECT wallet_type, status, COUNT(*) AS wallet_count
                    FROM points_wallet_identities
                    GROUP BY wallet_type, status
                    """
                ).fetchall()
                for row in wallet_identity_rows:
                    wallet_type = str(row["wallet_type"] or "unknown")
                    status = str(row["status"] or "unknown")
                    wallet_identity_counts.setdefault(wallet_type, {})
                    wallet_identity_counts[wallet_type][status] = int_value(row["wallet_count"])

            warnings = []
            if admin_missing or user_missing:
                warnings.append({
                    "code": "initial_grants_pending",
                    "severity": "warning",
                    "admin_missing": admin_missing,
                    "user_missing": user_missing,
                })
            if signup_missing:
                warnings.append({
                    "code": "signup_bonus_pending",
                    "severity": "info",
                    "missing": signup_missing,
                })
            if safe_mode.get("safe_mode"):
                warnings.append({
                    "code": "points_chain_safe_mode",
                    "severity": "critical",
                    "reason": safe_mode.get("reason") or "",
                })
            pending_transfers = int_value((transfer_by_status.get("pending") or {}).get("request_count"))
            if pending_transfers > 1000:
                warnings.append({
                    "code": "transfer_queue_backlog",
                    "severity": "warning",
                    "pending_requests": pending_transfers,
                })
            promo_balance = int_value((fund_balances.get("promo_fund") or {}).get("balance_points"))
            exchange_balance = int_value((fund_balances.get("exchange_fund") or {}).get("balance_points"))
            promo_low = int_value(policy_payload.get("promo_low_watermark"))
            promo_critical = int_value(policy_payload.get("promo_critical_watermark"))
            exchange_low = int_value(policy_payload.get("exchange_low_watermark"))
            exchange_critical = int_value(policy_payload.get("exchange_critical_watermark"))
            promo_known = "promo_fund" in fund_balances
            exchange_known = "exchange_fund" in fund_balances
            if promo_known and promo_critical and promo_balance <= promo_critical:
                warnings.append({"code": "promo_fund_critical", "severity": "critical", "balance_points": promo_balance})
            elif promo_known and promo_low and promo_balance <= promo_low:
                warnings.append({"code": "promo_fund_low", "severity": "warning", "balance_points": promo_balance})
            if exchange_known and exchange_critical and exchange_balance <= exchange_critical:
                warnings.append({"code": "exchange_fund_critical", "severity": "critical", "balance_points": exchange_balance})
            elif exchange_known and exchange_low and exchange_balance <= exchange_low:
                warnings.append({"code": "exchange_fund_low", "severity": "warning", "balance_points": exchange_balance})

            snapshot = {
                "ok": not any(item.get("severity") == "critical" for item in warnings),
                "snapshot_type": "points_operations_control",
                "generated_at": utc_now(),
                "bounded": True,
                "recent_limit": recent_limit,
                "chain_branch": branch,
                "initial_distribution": {
                    "policy": {
                        "admin_initial_points": ADMIN_INITIAL_POINTS,
                        "user_initial_points": USER_INITIAL_POINTS,
                        "signup_bonus_points": SIGNUP_BONUS_POINTS,
                    },
                    "eligible": {
                        "admins": admin_eligible,
                        "users": user_eligible,
                    },
                    "missing": {
                        "admin_initial_grants": admin_missing,
                        "user_initial_grants": user_missing,
                        "signup_bonus": signup_missing,
                    },
                    "grants": grant_summary,
                },
                "service_fee_connection": {
                    "catalog": {
                        "total_items": int_value(catalog_row["total"] if catalog_row else 0),
                        "enabled_items": int_value(catalog_row["enabled_count"] if catalog_row else 0),
                        "disabled_items": int_value(catalog_row["disabled_count"] if catalog_row else 0),
                    },
                    "charges_by_status": service_fee_by_status,
                    "ledger_spend": {
                        "ledger_entries": int_value(service_spend_row["ledger_entries"] if service_spend_row else 0),
                        "amount_points": int_value(service_spend_row["amount_points"] if service_spend_row else 0),
                        "latest_at": service_spend_row["latest_at"] if service_spend_row else None,
                    },
                },
                "private_chain": {
                    "ledger_entries": int_value(chain_counts["ledger_entries"] if chain_counts else 0),
                    "confirmed_entries": int_value(chain_counts["confirmed_entries"] if chain_counts else 0),
                    "unsealed_entries": int_value(chain_counts["unsealed_entries"] if chain_counts else 0),
                    "nonconfirmed_entries": int_value(chain_counts["nonconfirmed_entries"] if chain_counts else 0),
                    "latest_ledger_id": int_value(chain_counts["latest_ledger_id"] if chain_counts else 0),
                    "latest_ledger_at": chain_counts["latest_ledger_at"] if chain_counts else None,
                    "blocks": {
                        "block_count": int_value(blocks["block_count"] if blocks else 0),
                        "latest_block_number": int_value(blocks["latest_block_number"] if blocks else 0),
                        "latest_sealed_at": blocks["latest_sealed_at"] if blocks else None,
                    },
                    "transfer_requests_by_status": transfer_by_status,
                    "recent_unsealed": recent_unsealed,
                    "wallet_identities_by_type_status": wallet_identity_counts,
                },
                "economy_model": {
                    "policy": {
                        "policy_version": policy_row["policy_version"] if policy_row else "uninitialized",
                        "max_supply": int_value(policy_row["max_supply"] if policy_row else policy_payload.get("max_supply")),
                        "reserved_locked": int_value(policy_row["reserved_locked"] if policy_row else policy_payload.get("reserved_locked")),
                        "initial_mint": int_value(policy_row["initial_mint"] if policy_row else policy_payload.get("initial_mint")),
                        "promo_low_watermark": promo_low,
                        "promo_critical_watermark": promo_critical,
                        "exchange_low_watermark": exchange_low,
                        "exchange_critical_watermark": exchange_critical,
                    },
                    "latest_snapshot": dict(economy_snapshot) if economy_snapshot else {},
                    "fund_balances": fund_balances,
                    "incidents": incidents,
                },
                "exchange_operations": {
                    "exchange_fund": fund_balances.get("exchange_fund") or {},
                    "fund_watermark": {
                        "balance_points": exchange_balance,
                        "low_watermark": exchange_low,
                        "critical_watermark": exchange_critical,
                        "status": (
                            "critical" if exchange_known and exchange_critical and exchange_balance <= exchange_critical
                            else "low" if exchange_known and exchange_low and exchange_balance <= exchange_low
                            else "ok"
                        ),
                    },
                    "pending_transfers": pending_transfers,
                    "service_fee_settled_points": int_value((service_fee_by_status.get("settled") or {}).get("amount_points")),
                },
                "emergency": {
                    "safe_mode": safe_mode,
                    "governance_by_lifecycle_and_severity": governance_by_status,
                    "high_or_emergency_proposals": emergency_proposals,
                    "transaction_disputes_by_status": dispute_counts,
                    "dispute_schema_error": dispute_schema_error,
                    "active_address_freezes": active_freezes,
                    "active_provisional_freezes": active_provisional_freezes,
                },
                "warnings": warnings,
                "warning_count": len(warnings),
            }
            snapshot["management_timing"] = {
                "total_ms": round((_monotonic_time.perf_counter() - started) * 1000, 3),
                "phases": phases,
                "bounded": True,
            }
            conn.commit()
            return snapshot
        finally:
            conn.close()

    def verify_chain(self, *, include_financial=True):
        started = _monotonic_time.perf_counter()
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            result = self._verify_chain_on_conn(conn)
            verify_elapsed_ms = round((_monotonic_time.perf_counter() - started) * 1000, 3)
            if include_financial:
                financial_started = _monotonic_time.perf_counter()
                result["financial_invariants"] = self._financial_invariant_report_locked(conn)
                result["financial_ok"] = bool(result["financial_invariants"].get("ok"))
                financial_elapsed_ms = round((_monotonic_time.perf_counter() - financial_started) * 1000, 3)
            else:
                result["financial_ok"] = None
                financial_elapsed_ms = 0.0
            result["timing"] = {
                "verify_chain_ms": verify_elapsed_ms,
                "financial_invariants_ms": financial_elapsed_ms,
                "total_ms": round((_monotonic_time.perf_counter() - started) * 1000, 3),
                "include_financial": bool(include_financial),
            }
            conn.commit()
            return result
        finally:
            conn.close()

    def verify_chain_bounded_snapshot(self, *, recent_limit=1000):
        started = _monotonic_time.perf_counter()
        limit = max(50, min(5000, int(recent_limit or 1000)))
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            branch = self._canonical_branch_uuid(conn)
            errors = []
            counts_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS ledger_entries,
                    COALESCE(SUM(CASE WHEN chain_block_id IS NULL AND status='confirmed' THEN 1 ELSE 0 END), 0) AS unsealed_entries
                FROM points_ledger
                """
            ).fetchone()
            block_count = conn.execute("SELECT COUNT(*) AS c FROM points_chain_blocks").fetchone()["c"]
            audit_count = conn.execute("SELECT COUNT(*) AS c FROM points_chain_audit_logs").fetchone()["c"]
            wallet_count = conn.execute("SELECT COUNT(*) AS c FROM points_wallets").fetchone()["c"]
            recent_rows = list(conn.execute(
                """
                SELECT *
                FROM points_ledger
                WHERE chain_branch=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (branch, limit),
            ).fetchall())
            recent_rows.reverse()
            previous_hash = None
            previous_ledger = None
            if recent_rows:
                seed = conn.execute(
                    """
                    SELECT id, ledger_uuid, ledger_hash
                    FROM points_ledger
                    WHERE chain_branch=? AND id<?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (branch, int(recent_rows[0]["id"])),
                ).fetchone()
                if seed:
                    previous_hash = seed["ledger_hash"]
                    previous_ledger = seed
            for row in recent_rows:
                if row["previous_ledger_hash"] != previous_hash:
                    errors.append({
                        "type": "recent_ledger_previous_hash",
                        "severity": "critical",
                        "message": f"recent ledger #{row['id']} previous hash mismatch",
                        "chain_branch": branch,
                        "ledger_id": row["id"],
                        "ledger_uuid": row["ledger_uuid"],
                        "expected_previous_ledger_hash": previous_hash,
                        "actual_previous_ledger_hash": row["previous_ledger_hash"],
                        "previous_ledger_id": previous_ledger["id"] if previous_ledger else None,
                        "bounded": True,
                    })
                expected = compute_ledger_hash(row)
                if row["ledger_hash"] != expected:
                    errors.append({
                        "type": "recent_ledger_hash",
                        "severity": "critical",
                        "message": f"recent ledger #{row['id']} content hash mismatch",
                        "ledger_id": row["id"],
                        "ledger_uuid": row["ledger_uuid"],
                        "expected_ledger_hash": expected,
                        "actual_ledger_hash": row["ledger_hash"],
                        "bounded": True,
                    })
                previous_hash = row["ledger_hash"]
                previous_ledger = row
            block_rows = conn.execute(
                "SELECT * FROM points_chain_blocks ORDER BY block_number DESC LIMIT 20"
            ).fetchall()
            for block in block_rows:
                expected_block_hash = compute_block_hash(block)
                if expected_block_hash != block["block_hash"]:
                    errors.append({
                        "type": "recent_block_hash",
                        "severity": "critical",
                        "message": f"recent block #{block['block_number']} block hash mismatch",
                        "block_id": block["id"],
                        "block_number": block["block_number"],
                        "expected_block_hash": expected_block_hash,
                        "actual_block_hash": block["block_hash"],
                        "bounded": True,
                    })
                signature = conn.execute(
                    """
                    SELECT * FROM points_chain_block_signatures
                    WHERE block_id=? AND node_id='single-node'
                    """,
                    (block["id"],),
                ).fetchone()
                if not signature:
                    errors.append({
                        "type": "recent_block_signature_missing",
                        "severity": "high",
                        "message": f"recent block #{block['block_number']} signature missing",
                        "block_id": block["id"],
                        "block_number": block["block_number"],
                        "bounded": True,
                    })
                elif (
                    signature["signature_algorithm"] != "hmac-sha256"
                    or signature["public_key_fingerprint"] != self._node_fingerprint()
                    or signature["signature"] != self._sign_block(block)
                ):
                    errors.append({
                        "type": "recent_block_signature_invalid",
                        "severity": "critical",
                        "message": f"recent block #{block['block_number']} signature invalid",
                        "block_id": block["id"],
                        "block_number": block["block_number"],
                        "bounded": True,
                    })
            safe_mode = self._safe_mode_status(conn)
            unsealed_entries = int(counts_row["unsealed_entries"] or 0)
            result = {
                "ok": not errors and not bool(safe_mode.get("safe_mode")),
                "errors": errors[:100],
                "error_count": len(errors),
                "bounded": True,
                "verification_mode": "bounded_recent_snapshot",
                "recent_limit": limit,
                "recent_checked": len(recent_rows),
                "safe_mode": safe_mode,
                "financial_ok": None,
                "counts": {
                    "ledger_entries": int(counts_row["ledger_entries"] or 0),
                    "sealed_blocks": int(block_count or 0),
                    "unsealed_entries": unsealed_entries,
                    "pc1_canonical_entries": None,
                    "pc1_unsealed_entries": unsealed_entries,
                    "pc0_operational_entries": None,
                    "pc0_operational_unsealed_entries": None,
                    "pc1_pc0_split_source": "omitted_in_bounded_snapshot",
                    "audit_events": int(audit_count or 0),
                    "wallets": int(wallet_count or 0),
                },
                "timing": {
                    "total_ms": round((_monotonic_time.perf_counter() - started) * 1000, 3),
                    "include_financial": False,
                    "bounded": True,
                },
            }
            return result
        finally:
            conn.close()

    def _explorer_acceleration_summary(self, conn, ledger_uuid):
        rows = conn.execute(
            """
            SELECT *
            FROM points_chain_acceleration_requests
            WHERE ledger_uuid=? AND status='accepted'
            ORDER BY id ASC
            """,
            (str(ledger_uuid or ""),),
        ).fetchall()
        total_fee = sum(int(row["fee_points"] or 0) for row in rows)
        latest = dict(rows[-1]) if rows else None
        return {
            "count": len(rows),
            "total_fee_points": total_fee,
            "latest_request": latest,
        }

    def _explorer_network_fee_state(self, conn):
        if not conn:
            congestion = 0.0
            return {
                "congestion_ratio": congestion,
                "congestion_label": "idle",
                "pending_transfer_count": 0,
                "unsealed_ledger_count": 0,
                "recent_ledger_count": 0,
                "base_fee_points": 1,
                "suggested_priority_fee_points": EXPLORER_ACCELERATION_REFERENCE_FEE_POINTS,
                "suggested_total_fee_points": 1 + EXPLORER_ACCELERATION_REFERENCE_FEE_POINTS,
            }
        cutoff = datetime.fromtimestamp(time.time() - 5 * 60, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        pending = 0
        unsealed = 0
        recent = 0
        branch = self._main_branch_uuid()
        try:
            branch = self._canonical_branch_uuid(conn)
            if table_columns(conn, "points_chain_transfer_requests"):
                pending = int(conn.execute(
                    "SELECT COUNT(*) FROM points_chain_transfer_requests WHERE status='pending' AND chain_branch=?",
                    (branch,),
                ).fetchone()[0] or 0)
            if table_columns(conn, "points_ledger"):
                # This estimate feeds public explorer ETA and fee guidance. It
                # should stay bounded even when the ledger is large; exact pc0
                # vs pc1 classification belongs to verify/report paths.
                unsealed = int(conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM points_ledger
                    WHERE status='confirmed'
                      AND chain_block_id IS NULL
                      AND chain_branch=?
                    """,
                    (branch,),
                ).fetchone()[0] or 0)
                recent = int(conn.execute(
                    "SELECT COUNT(*) FROM points_ledger WHERE created_at>=? AND chain_branch=?",
                    (cutoff, branch),
                ).fetchone()[0] or 0)
        except Exception:
            pending = unsealed = recent = 0
        pending_pressure = min(1.0, pending / 50.0)
        unsealed_pressure = min(1.0, max(0, unsealed - 25) / 200.0)
        recent_pressure = min(1.0, max(0, recent - 100) / 500.0)
        congestion = round(max(pending_pressure, unsealed_pressure, recent_pressure), 4)
        if congestion >= 0.75:
            label = "congested"
        elif congestion >= 0.35:
            label = "busy"
        elif congestion > 0:
            label = "normal"
        else:
            label = "idle"
        base_fee = max(1, int(round(1 + congestion * 9)))
        priority_fee = max(
            EXPLORER_ACCELERATION_REFERENCE_FEE_POINTS,
            int(round(EXPLORER_ACCELERATION_REFERENCE_FEE_POINTS * (1 + congestion * 4))),
        )
        return {
            "congestion_ratio": congestion,
            "chain_branch": branch,
            "congestion_label": label,
            "pending_transfer_count": pending,
            "unsealed_ledger_count": unsealed,
            "recent_ledger_count": recent,
            "base_fee_points": base_fee,
            "suggested_priority_fee_points": priority_fee,
            "suggested_total_fee_points": base_fee + priority_fee,
        }

    def _explorer_finality_estimate(self, fee_points=0, *, conn=None, network_state=None):
        fee = max(0, min(EXPLORER_MAX_ACCELERATION_FEE_POINTS, int(fee_points or 0)))
        network = network_state or self._explorer_network_fee_state(conn)
        congestion = max(0.0, min(1.0, float(network.get("congestion_ratio") or 0.0)))
        base_min = round(EXPLORER_BASE_FINALITY_MIN_SECONDS * (1 + congestion * 1.5))
        base_max = round(EXPLORER_BASE_FINALITY_MAX_SECONDS * (1 + congestion * 1.5))
        reference_fee = max(
            1,
            int(network.get("suggested_priority_fee_points") or EXPLORER_ACCELERATION_REFERENCE_FEE_POINTS),
        )
        # Simulate common fee-market behavior: higher priority fee moves a
        # transaction toward faster inclusion, but with diminishing returns.
        speedup_ratio = fee / (fee + reference_fee) if fee > 0 else 0.0
        min_seconds = round(
            base_min
            - (base_min - EXPLORER_ACCELERATED_FINALITY_MIN_SECONDS) * speedup_ratio
        )
        max_seconds = round(
            base_max
            - (base_max - EXPLORER_ACCELERATED_FINALITY_MAX_SECONDS) * speedup_ratio
        )
        if max_seconds < min_seconds + 15:
            max_seconds = min_seconds + 15
        return {
            "target_proved_count": EXPLORER_FINALITY_PROVED_COUNT,
            "base_seconds_min": EXPLORER_BASE_FINALITY_MIN_SECONDS,
            "base_seconds_max": EXPLORER_BASE_FINALITY_MAX_SECONDS,
            "network_base_seconds_min": base_min,
            "network_base_seconds_max": base_max,
            "minimum_seconds_min": EXPLORER_ACCELERATED_FINALITY_MIN_SECONDS,
            "minimum_seconds_max": EXPLORER_ACCELERATED_FINALITY_MAX_SECONDS,
            "fee_points": fee,
            "fee_model": "priority_fee_diminishing_ratio_v2",
            "fee_reference_points": reference_fee,
            "speedup_ratio": round(speedup_ratio, 4),
            "network_fee_state": network,
            "estimated_seconds_min": min_seconds,
            "estimated_seconds_max": max_seconds,
        }

    def _explorer_finality_schedule(self, ledger, estimate):
        target = EXPLORER_FINALITY_PROVED_COUNT
        min_seconds = max(1, int(estimate.get("estimated_seconds_min") or EXPLORER_BASE_FINALITY_MIN_SECONDS))
        max_seconds = max(min_seconds, int(estimate.get("estimated_seconds_max") or EXPLORER_BASE_FINALITY_MAX_SECONDS))
        seed = sha256_text(f"{ledger['ledger_uuid']}:{ledger['ledger_hash']}:{ledger['created_at']}")
        span = max(0, max_seconds - min_seconds)
        settlement_seconds = min_seconds + (int(seed[:8], 16) % (span + 1 if span else 1))
        first_proof = 4 + (int(seed[8:10], 16) % 9)
        first_proof = min(first_proof, max(1, settlement_seconds // 3))
        remaining = max(target - 1, settlement_seconds - first_proof)
        weights = []
        for index in range(target - 1):
            offset = 10 + (index * 2)
            weights.append(70 + (int(seed[offset:offset + 2] or seed[:2], 16) % 61))
        total_weight = max(1, sum(weights))
        marks = [first_proof]
        elapsed = float(first_proof)
        for weight in weights:
            elapsed += remaining * weight / total_weight
            marks.append(int(round(elapsed)))
        marks[-1] = settlement_seconds
        for index in range(1, len(marks)):
            if marks[index] <= marks[index - 1]:
                marks[index] = marks[index - 1] + 1
        if marks[-1] > settlement_seconds:
            overflow = marks[-1] - settlement_seconds
            marks = [max(1, mark - round(overflow * (index / max(1, target - 1)))) for index, mark in enumerate(marks)]
            marks[-1] = settlement_seconds
        return {
            "settlement_seconds": settlement_seconds,
            "first_proof_seconds": first_proof,
            "proof_marks": marks,
        }

    def _explorer_finality_from_created(self, *, created_at, estimate, schedule, sealed=False, fee_policy=None, acceleration_fee_points=0):
        fee_policy = fee_policy or {}
        created = parse_utc_timestamp(created_at)
        elapsed = 0
        if created:
            elapsed = max(0, int((datetime.now(timezone.utc) - created).total_seconds()))
        if sealed:
            proved_count = EXPLORER_FINALITY_PROVED_COUNT
            finality_status = "sealed"
            eta_seconds = 0
            next_proof_eta_seconds = 0
        else:
            marks = schedule["proof_marks"]
            proved_count = sum(1 for mark in marks if elapsed >= mark)
            finality_status = "proved" if proved_count >= EXPLORER_FINALITY_PROVED_COUNT else "pending"
            eta_seconds = max(0, schedule["settlement_seconds"] - elapsed)
            next_mark = next((mark for mark in marks if mark > elapsed), schedule["settlement_seconds"])
            next_proof_eta_seconds = max(0, next_mark - elapsed)
        return {
            **estimate,
            "proved_count": proved_count,
            "proved_remaining": max(0, EXPLORER_FINALITY_PROVED_COUNT - proved_count),
            "finality_status": finality_status,
            "block_status": "sealed" if sealed else "unsealed",
            "elapsed_seconds": elapsed,
            "eta_seconds": eta_seconds,
            "next_proof_eta_seconds": next_proof_eta_seconds,
            "settlement_seconds": schedule["settlement_seconds"],
            "first_proof_seconds": schedule["first_proof_seconds"],
            "finality_simulation": "deterministic_proved_schedule_v1",
            "transaction_fee_points": 0 if fee_policy.get("base_fee_exempt") else int(acceleration_fee_points or 0),
            "gas_price_points_per_proved": (
                0
                if fee_policy.get("base_fee_exempt")
                else round((int(acceleration_fee_points or 0) or 1) / EXPLORER_FINALITY_PROVED_COUNT, 4)
            ),
            "chain_fee_policy": fee_policy,
            "human_rule": EXPLORER_CHAIN_HUMAN_RULE,
        }

    def _explorer_internal_hot_wallet_finality(self, *, block_status="internal_ledger", human_rule=None):
        rule = human_rule or EXPLORER_INTERNAL_HOT_WALLET_HUMAN_RULE
        return {
            "target_proved_count": 0,
            "base_seconds_min": 0,
            "base_seconds_max": 0,
            "network_base_seconds_min": 0,
            "network_base_seconds_max": 0,
            "minimum_seconds_min": 0,
            "minimum_seconds_max": 0,
            "fee_points": 0,
            "fee_model": "internal_ledger_no_priority_fee_v1",
            "fee_reference_points": 0,
            "speedup_ratio": 0,
            "network_fee_state": {
                "congestion_ratio": 0,
                "congestion_label": "internal",
                "pending_transfer_count": 0,
                "unsealed_ledger_count": 0,
                "recent_ledger_count": 0,
                "base_fee_points": 0,
                "suggested_priority_fee_points": 0,
                "suggested_total_fee_points": 0,
            },
            "estimated_seconds_min": 0,
            "estimated_seconds_max": 0,
            "proved_count": 0,
            "proved_remaining": 0,
            "finality_status": "internal_settled",
            "block_status": block_status,
            "elapsed_seconds": 0,
            "eta_seconds": 0,
            "next_proof_eta_seconds": 0,
            "settlement_seconds": 0,
            "first_proof_seconds": 0,
            "finality_simulation": "internal_hot_wallet_ledger_v1",
            "transaction_fee_points": 0,
            "gas_price_points_per_proved": 0,
            "chain_fee_policy": {
                "base_fee_exempt": True,
                "base_fee_destination_fund_key": "burn",
                "base_fee_destination_label": "BURN 銷毀錢包",
                "acceleration_allowed": False,
                "exemption_reason": rule,
                "manual_official_wallet_ops_are_auto": False,
            },
            "human_rule": rule,
        }

    def _ledger_uses_internal_hot_wallet_rail(self, conn, ledger):
        try:
            raw_public_metadata = ledger["public_metadata_json"]
        except Exception:
            raw_public_metadata = ledger.get("public_metadata_json") if hasattr(ledger, "get") else None
        if raw_public_metadata is None and hasattr(ledger, "get"):
            raw_public_metadata = ledger.get("public_metadata")
        public_metadata = raw_public_metadata if isinstance(raw_public_metadata, dict) else _json_loads(raw_public_metadata, {})
        flow = self._ledger_wallet_flow_for_read(conn, ledger)
        settlement_rail = self._ledger_settlement_rail_for_read(
            conn,
            ledger,
            flow=flow,
            public_metadata=public_metadata,
        )
        if settlement_rail in {"internal_hot_wallet", "internal_system_burn", "deposit_bridge_credit", "withdrawal_bridge_refund"}:
            return True
        chain_required = public_metadata.get("chain_required", flow.get("chain_required"))
        approval_required = public_metadata.get("approval_required", flow.get("approval_required"))
        if chain_required is False and approval_required is False:
            return True
        source = str(flow.get("source_wallet_address") or "").strip().lower()
        destination = str(flow.get("destination_wallet_address") or "").strip().lower()
        return bool(source and destination and is_pc0_internal_address(source) and is_pc0_internal_address(destination))

    def _ledger_settlement_rail_for_read(self, conn, ledger, *, flow=None, public_metadata=None):
        if public_metadata is None:
            try:
                raw_public_metadata = ledger["public_metadata_json"]
            except Exception:
                raw_public_metadata = ledger.get("public_metadata_json") if hasattr(ledger, "get") else None
            if raw_public_metadata is None and hasattr(ledger, "get"):
                raw_public_metadata = ledger.get("public_metadata")
            public_metadata = raw_public_metadata if isinstance(raw_public_metadata, dict) else _json_loads(raw_public_metadata, {})
        flow = flow if isinstance(flow, dict) else self._ledger_wallet_flow_for_read(conn, ledger)
        settlement_rail = str(public_metadata.get("settlement_rail") or flow.get("settlement_rail") or "").strip()
        if settlement_rail:
            return settlement_rail
        request_uuid = str(public_metadata.get("pending_request_uuid") or public_metadata.get("request_uuid") or "").strip()
        tx_group_hash = str(public_metadata.get("tx_group_hash") or "").strip()
        try:
            reference_type = str(ledger["reference_type"] or "")
            reference_id = str(ledger["reference_id"] or "").strip()
            ledger_uuid = str(ledger["ledger_uuid"] or "").strip()
        except Exception:
            reference_type = str(ledger.get("reference_type") or "") if hasattr(ledger, "get") else ""
            reference_id = str(ledger.get("reference_id") or "").strip() if hasattr(ledger, "get") else ""
            ledger_uuid = str(ledger.get("ledger_uuid") or "").strip() if hasattr(ledger, "get") else ""
        if not tx_group_hash and reference_type == "wallet_transfer":
            tx_group_hash = reference_id
        if not any((request_uuid, tx_group_hash, ledger_uuid)):
            return ""
        try:
            req = conn.execute(
                """
                SELECT settlement_rail
                FROM points_chain_transfer_requests
                WHERE request_uuid=?
                   OR tx_group_hash=?
                   OR transfer_out_ledger_uuid=?
                   OR transfer_in_ledger_uuid=?
                   OR fee_ledger_uuid=?
                LIMIT 1
                """,
                (request_uuid, tx_group_hash, ledger_uuid, ledger_uuid, ledger_uuid),
            ).fetchone()
        except Exception:
            req = None
        return str(req["settlement_rail"] or "").strip() if req else ""

    def _ledger_is_pc1_canonical_sealable(self, conn, ledger):
        if str(ledger["status"] if "status" in ledger.keys() else "confirmed") != "confirmed":
            return False
        raw_public_metadata = ledger["public_metadata_json"] if "public_metadata_json" in ledger.keys() else None
        public_metadata = _json_loads(raw_public_metadata, {})
        flow = self._ledger_wallet_flow_for_read(conn, ledger)
        settlement_rail = self._ledger_settlement_rail_for_read(
            conn,
            ledger,
            flow=flow,
            public_metadata=public_metadata,
        )
        if settlement_rail in PC0_OPERATIONAL_SETTLEMENT_RAILS:
            return False
        if settlement_rail in PC1_CANONICAL_SETTLEMENT_RAILS:
            return True
        chain_required_value = public_metadata.get("chain_required", flow.get("chain_required"))
        if chain_required_value is not None and not _metadata_bool(chain_required_value, default=True):
            return False
        addresses = [
            str(flow.get("source_wallet_address") or "").strip().lower(),
            str(flow.get("destination_wallet_address") or "").strip().lower(),
            str(flow.get("target_wallet_address") or "").strip().lower(),
        ]
        if any(is_pc0_internal_address(address) for address in addresses if address):
            return False
        action_type = str(ledger["action_type"] or "")
        if self._is_configured_auto_distribution_action(action_type):
            return False
        return True

    def _pc1_unsealed_ledger_rows_locked(self, conn, *, branch, limit=100):
        rows = conn.execute(
            """
            SELECT *
            FROM points_ledger
            WHERE status='confirmed' AND chain_block_id IS NULL AND chain_branch=?
            ORDER BY id ASC
            LIMIT ?
            """,
            (branch, max(5000, min(50000, max(1, int(limit or 100)) * 20))),
        ).fetchall()
        selected = []
        for row in rows:
            if self._ledger_is_pc1_canonical_sealable(conn, row):
                selected.append(row)
                if len(selected) >= max(1, int(limit or 100)):
                    break
        return selected

    def _pc1_chain_ledger_count_snapshot_locked(self, conn, *, branch=None):
        branch = str(branch or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        rows = conn.execute(
            """
            SELECT *
            FROM points_ledger
            WHERE status='confirmed' AND chain_branch=?
            ORDER BY id ASC
            """,
            (branch,),
        ).fetchall()
        canonical_total = 0
        canonical_unsealed = 0
        internal_total = 0
        internal_unsealed = 0
        for row in rows:
            is_pc1 = self._ledger_is_pc1_canonical_sealable(conn, row)
            if is_pc1:
                canonical_total += 1
                if not row["chain_block_id"]:
                    canonical_unsealed += 1
            else:
                internal_total += 1
                if not row["chain_block_id"]:
                    internal_unsealed += 1
        return {
            "pc1_canonical_entries": canonical_total,
            "pc1_unsealed_entries": canonical_unsealed,
            "pc0_operational_entries": internal_total,
            "pc0_operational_unsealed_entries": internal_unsealed,
        }

    def _explorer_finality_for_ledger(self, conn, ledger, *, network_state=None):
        if self._ledger_uses_internal_hot_wallet_rail(conn, ledger):
            return {
                **self._explorer_internal_hot_wallet_finality(
                    block_status="sealed" if ledger["chain_block_id"] else "internal_ledger"
                ),
                "accelerated": False,
                "acceleration_request_count": 0,
                "acceleration_fee_paid_points": 0,
                "acceleration_fee_destination_fund_key": "",
                "acceleration_fee_destination_label": "",
                "latest_acceleration_request": None,
            }
        accel = self._explorer_acceleration_summary(conn, ledger["ledger_uuid"])
        estimate = self._explorer_finality_estimate(
            accel["total_fee_points"],
            conn=conn,
            network_state=network_state,
        )
        fee_policy = self._explorer_chain_fee_policy(ledger)
        schedule = self._explorer_finality_schedule(ledger, estimate)
        finality = self._explorer_finality_from_created(
            created_at=ledger["created_at"],
            estimate=estimate,
            schedule=schedule,
            sealed=bool(ledger["chain_block_id"]),
            fee_policy=fee_policy,
            acceleration_fee_points=accel["total_fee_points"],
        )
        return {
            **finality,
            "accelerated": accel["count"] > 0,
            "acceleration_request_count": accel["count"],
            "acceleration_fee_paid_points": accel["total_fee_points"],
            "acceleration_fee_destination_fund_key": "burn" if accel["total_fee_points"] else "",
            "acceleration_fee_destination_label": "BURN 銷毀錢包" if accel["total_fee_points"] else "",
            "latest_acceleration_request": accel["latest_request"],
        }

    def _explorer_find_ledger(self, conn, ref):
        ref = str(ref or "").strip()
        if not ref:
            return None
        return conn.execute(
            """
            SELECT *
            FROM points_ledger
            WHERE ledger_uuid=? OR ledger_hash=?
            LIMIT 1
            """,
            (ref, ref),
        ).fetchone()

    def _explorer_find_economy_event(self, conn, ref):
        ref = str(ref or "").strip()
        if not ref:
            return None
        return conn.execute(
            """
            SELECT *
            FROM points_economy_events
            WHERE event_uuid=? OR event_hash=? OR request_hash=? OR idempotency_key=?
            LIMIT 1
            """,
            (ref, ref, ref, ref),
        ).fetchone()

    def _explorer_find_bridge_event(self, conn, ref):
        ref = str(ref or "").strip()
        if not ref or not table_columns(conn, "points_chain_bridge_events"):
            return None
        return conn.execute(
            """
            SELECT *
            FROM points_chain_bridge_events
            WHERE bridge_uuid=?
               OR chain_tx_hash=?
               OR internal_ledger_uuid=?
            LIMIT 1
            """,
            (ref, ref, ref),
        ).fetchone()

    def _explorer_layer_for_rail(self, settlement_rail, *, source_address="", destination_address=""):
        rail = str(settlement_rail or "").strip()
        if rail in PC0_OPERATIONAL_SETTLEMENT_RAILS:
            return "pc0"
        if rail in PC1_CANONICAL_SETTLEMENT_RAILS:
            return "pc1"
        source = str(source_address or "").strip().lower()
        destination = str(destination_address or "").strip().lower()
        if is_pc0_internal_address(source) or is_pc0_internal_address(destination):
            return "pc0"
        if rail.startswith("deposit_bridge") or rail.startswith("withdrawal_bridge"):
            return "bridge"
        return "pc1"

    def _explorer_asset_type_for_layer(self, layer, settlement_rail=""):
        layer = str(layer or "")
        rail = str(settlement_rail or "")
        if layer == "pc0":
            return "Wrapped Operational Representation"
        if layer == "bridge":
            return "Cross-Ledger Settlement Event"
        if rail == "cold_chain":
            return "Canonical Settlement Asset"
        return "Canonical Reserve Accounting"

    def _explorer_cross_references_for_ledger(self, conn, row, *, settlement_rail=""):
        refs = {}
        ledger_uuid = str(row["ledger_uuid"] if "ledger_uuid" in row.keys() else "").strip()
        reference_id = str(row["reference_id"] if "reference_id" in row.keys() else "").strip()
        if settlement_rail == "deposit_bridge_credit" or str(row["reference_type"] if "reference_type" in row.keys() else "") == "deposit_bridge":
            bridge = conn.execute(
                """
                SELECT bridge_uuid, chain_tx_hash, source_address, destination_address, hot_wallet_address, status
                FROM points_chain_bridge_events
                WHERE internal_ledger_uuid=? OR chain_tx_hash=?
                LIMIT 1
                """,
                (ledger_uuid, reference_id),
            ).fetchone()
            if bridge:
                refs["bridge_event_uuid"] = bridge["bridge_uuid"]
                refs["bridge_event_query"] = bridge["bridge_uuid"]
                refs["pc1_settlement_tx"] = bridge["chain_tx_hash"]
                refs["pc1_deposit_address"] = bridge["destination_address"]
                refs["pc0_wrapped_credit"] = ledger_uuid
                refs["pc0_hot_wallet"] = bridge["hot_wallet_address"]
                refs["settlement_state"] = bridge["status"]
        return refs

    def _explorer_public_bridge_event(self, conn, row):
        metadata = _json_loads(row["metadata_json"], {})
        ledger_uuid = str(row["internal_ledger_uuid"] or "").strip()
        internal_tx = None
        if ledger_uuid:
            ledger = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (ledger_uuid,)).fetchone()
            if ledger:
                internal_tx = self._explorer_public_ledger(conn, ledger)
        confirmations = int(row["confirmations"] or 0)
        required = int(row["required_confirmations"] or 20)
        risk_status = str(row["risk_status"] or "").strip().lower()
        status = str(row["status"] or "").strip().lower()
        chain_destination_is_pc0 = is_pc0_internal_address(row["destination_address"])
        valid = (
            status in {"pending", "confirmed", "credited", "failed", "refunded"}
            and not chain_destination_is_pc0
            and (status != "credited" or (risk_status == "accepted" and confirmations >= required and bool(internal_tx)))
        )
        return {
            "kind": "bridge",
            "layer": "bridge",
            "asset_type": "Cross-Ledger Settlement Event",
            "bridge_event": {
                "asset_type": "Cross-Ledger Settlement Event",
                "bridge_uuid": row["bridge_uuid"],
                "bridge_type": row["bridge_type"],
                "chain": row["chain"],
                "chain_tx_hash": row["chain_tx_hash"],
                "source_address": row["source_address"],
                "destination_address": row["destination_address"],
                "hot_wallet_address": row["hot_wallet_address"],
                "amount_points": int(row["amount_points"] or 0),
                "network_fee_points": int(row["network_fee_points"] or 0),
                "confirmations": confirmations,
                "required_confirmations": required,
                "risk_status": row["risk_status"],
                "status": row["status"],
                "settlement_state": row["status"],
                "internal_ledger_uuid": ledger_uuid,
                "created_at": row["created_at"],
                "confirmed_at": row["confirmed_at"] if "confirmed_at" in row.keys() else "",
                "credited_at": row["credited_at"] if "credited_at" in row.keys() else "",
                "metadata": metadata,
                "invariant_status": "valid" if valid else "invalid",
                "chain_destination_is_pc0": chain_destination_is_pc0,
                "pc1_settlement_tx": row["chain_tx_hash"],
                "pc0_wrapped_credit": ledger_uuid,
                "pc0_hot_wallet": row["hot_wallet_address"],
                "internal_transaction": internal_tx,
            },
        }

    def _explorer_economy_event_flow(self, row):
        source_fund = str(row["source_fund_key"] or "")
        destination_fund = str(row["destination_fund_key"] or "")
        source_label = self._economy_fund_flow_ref(source_fund)[0] if source_fund else ""
        destination_label = self._economy_fund_flow_ref(destination_fund)[0] if destination_fund else ""
        return {
            "walletized": True,
            "system_fund_event": True,
            "source_fund_key": source_fund,
            "source_wallet_address": str(row["source_address"] or ""),
            "source_label": source_label,
            "destination_fund_key": destination_fund,
            "destination_wallet_address": str(row["destination_address"] or ""),
            "destination_label": destination_label,
        }

    def _economy_event_is_genesis_allocation(self, row):
        if str(row["source_fund_key"] or "") != "mint":
            return False
        if str(row["transaction_type"] or "") in {"treasury_allocation", "promo_allocation", "exchange_allocation"}:
            return True
        metadata = _json_loads(row["metadata_json"], {})
        return bool(isinstance(metadata, dict) and metadata.get("bootstrap"))

    def _explorer_economy_genesis_rows(self, conn, *, branch_uuid=None):
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        rows = conn.execute(
            """
            SELECT *
            FROM points_economy_events
            WHERE status='confirmed' AND chain_branch=? AND source_fund_key='mint'
            ORDER BY id ASC
            """,
            (branch,),
        ).fetchall()
        return [row for row in rows if self._economy_event_is_genesis_allocation(row)]

    def _explorer_economy_genesis_block_summary(self, conn, *, branch_uuid=None):
        rows = self._explorer_economy_genesis_rows(conn, branch_uuid=branch_uuid)
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        event_hashes = [str(row["event_hash"] or "") for row in rows]
        block_hash = sha256_text(canonical_json({
            "block_type": "economy_genesis",
            "chain_branch": branch,
            "event_hashes": event_hashes,
        }))
        merkle_root = sha256_text(canonical_json(event_hashes))
        sealed_at = rows[0]["created_at"] if rows else ""
        return {
            "block_number": 0,
            "block_height": 0,
            "block_hash": block_hash,
            "previous_block_hash": "",
            "merkle_root": merkle_root,
            "ledger_count": len(rows),
            "transaction_count": len(rows),
            "first_ledger_id": 0,
            "last_ledger_id": 0,
            "sealed_at": sealed_at,
            "timestamp": sealed_at,
            "seal_status": "virtual_economy_genesis",
            "anchor_status": "economy_event_log",
        }

    def _explorer_economy_event_block(self, conn, row):
        if not self._economy_event_is_genesis_allocation(row):
            return None
        return self._explorer_economy_genesis_block_summary(
            conn,
            branch_uuid=row["chain_branch"] if "chain_branch" in row.keys() else self._main_branch_uuid(),
        )

    def _explorer_finality_for_economy_event(self, conn, row):
        estimate = self._explorer_finality_estimate(0, conn=conn)
        return {
            **estimate,
            "proved_count": EXPLORER_FINALITY_PROVED_COUNT,
            "proved_remaining": 0,
            "finality_status": "sealed" if self._economy_event_is_genesis_allocation(row) else "proved",
            "block_status": "economy_event_log",
            "elapsed_seconds": 0,
            "eta_seconds": 0,
            "next_proof_eta_seconds": 0,
            "settlement_seconds": 0,
            "first_proof_seconds": 0,
            "finality_simulation": "append_only_economy_event_log_v1",
            "transaction_fee_points": 0,
            "gas_price_points_per_proved": 0,
            "chain_fee_policy": {
                "base_fee_exempt": True,
                "base_fee_destination_fund_key": "burn",
                "base_fee_destination_label": "BURN 銷毀錢包",
                "acceleration_allowed": False,
                "exemption_reason": "system fund mint/economy events are protocol accounting entries",
                "manual_official_wallet_ops_are_auto": True,
            },
            "human_rule": "系統 fund event 為 append-only economy event；MINT/genesis allocation 以 virtual genesis block 呈現",
        }

    def _explorer_public_economy_event(self, conn, row):
        metadata = _json_loads(row["metadata_json"], {})
        source_address = str(row["source_address"] or "")
        destination_address = str(row["destination_address"] or "")
        layer = self._explorer_layer_for_rail(
            "economy_event",
            source_address=source_address,
            destination_address=destination_address,
        )
        return {
            "layer": layer,
            "asset_type": self._explorer_asset_type_for_layer(layer, "economy_event"),
            "event_uuid": row["event_uuid"],
            "ledger_uuid": row["event_uuid"],
            "chain_branch": row["chain_branch"] if "chain_branch" in row.keys() else "main",
            "branch": self._branch_metadata(conn, row["chain_branch"] if "chain_branch" in row.keys() else "main"),
            "event_hash": row["event_hash"],
            "ledger_hash": row["event_hash"],
            "previous_event_hash": row["previous_event_hash"],
            "previous_ledger_hash": row["previous_event_hash"],
            "public_account_id": source_address,
            "currency_type": DISPLAY_CURRENCY,
            "direction": "transfer_out",
            "amount": int(row["amount"] or 0),
            "action_type": row["transaction_type"],
            "reference_type": "points_economy_event",
            "reference_id": row["event_uuid"],
            "reason": public_currency_text(metadata.get("reason") or row["transaction_type"] or ""),
            "input_data": metadata,
            "status": row["status"],
            "created_at": row["created_at"],
            "chain_block_id": None,
            "wallet_flow": self._explorer_economy_event_flow(row),
            "source_wallet_address": source_address,
            "destination_wallet_address": destination_address,
            "block": self._explorer_economy_event_block(conn, row),
            "finality": self._explorer_finality_for_economy_event(conn, row),
        }

    def _explorer_public_ledger(self, conn, row, *, network_state=None):
        flow = self._ledger_wallet_flow_for_read(conn, row)
        public_metadata = _json_loads(row["public_metadata_json"], {})
        settlement_rail = self._ledger_settlement_rail_for_read(
            conn,
            row,
            flow=flow,
            public_metadata=public_metadata,
        )
        layer = self._explorer_layer_for_rail(
            settlement_rail,
            source_address=flow.get("source_wallet_address"),
            destination_address=flow.get("destination_wallet_address"),
        )
        input_data = {
            key: value
            for key, value in public_metadata.items()
            if key not in {"wallet_flow_snapshot"}
        } if isinstance(public_metadata, dict) else {}
        block = None
        if row["chain_block_id"]:
            block_row = conn.execute("SELECT * FROM points_chain_blocks WHERE id=?", (row["chain_block_id"],)).fetchone()
            if block_row:
                block = {
                    "block_number": int(block_row["block_number"]),
                    "block_hash": block_row["block_hash"],
                    "sealed_at": block_row["sealed_at"],
                    "ledger_count": int(block_row["ledger_count"]),
                }
        return {
            "layer": layer,
            "asset_type": self._explorer_asset_type_for_layer(layer, settlement_rail),
            "settlement_rail": settlement_rail,
            "ledger_uuid": row["ledger_uuid"],
            "chain_branch": row["chain_branch"] if "chain_branch" in row.keys() else "main",
            "branch": self._branch_metadata(conn, row["chain_branch"] if "chain_branch" in row.keys() else "main"),
            "ledger_hash": row["ledger_hash"],
            "previous_ledger_hash": row["previous_ledger_hash"],
            "public_account_id": row["public_account_id"],
            "currency_type": DISPLAY_CURRENCY,
            "direction": row["direction"],
            "amount": int(row["amount"] or 0),
            "action_type": row["action_type"],
            "reference_type": row["reference_type"],
            "reference_id": row["reference_id"],
            "reason": public_currency_text(row["reason"] or ""),
            "input_data": input_data,
            "status": row["status"],
            "created_at": row["created_at"],
            "chain_block_id": row["chain_block_id"],
            "wallet_flow": flow,
            "cross_references": self._explorer_cross_references_for_ledger(conn, row, settlement_rail=settlement_rail),
            "block": block,
            "finality": self._explorer_finality_for_ledger(conn, row, network_state=network_state),
        }

    def explorer_transaction(self, ref):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            row = self._explorer_find_ledger(conn, ref)
            if row:
                return {"kind": "transaction", "transaction": self._explorer_public_ledger(conn, row)}
            event = self._explorer_find_economy_event(conn, ref)
            if event:
                return {"kind": "transaction", "transaction": self._explorer_public_economy_event(conn, event)}
            req = self._explorer_find_transfer_request(conn, ref)
            if not req:
                return None
            if str(req["status"] or "") == "pending":
                conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                req = self._explorer_find_transfer_request(conn, ref)
                req = self._maybe_finalize_transfer_request_locked(
                    conn,
                    req,
                    actor={"role": "system", "username": "pointschain", "id": None},
                )
                conn.commit()
            return {"kind": "transaction", "transaction": self._transfer_request_public_payload(conn, req)}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def explorer_bridge_event(self, ref):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            row = self._explorer_find_bridge_event(conn, ref)
            if not row:
                return None
            return self._explorer_public_bridge_event(conn, row)
        finally:
            conn.close()

    def _explorer_fund_key_for_address(self, address):
        address = str(address or "").strip().lower()
        for fund_key in ("mint", "official_treasury", "promo_fund", "exchange_fund", "burn"):
            if economy_fund_address(self.chain_secret, fund_key) == address:
                return fund_key
        return ""

    def _official_transfer_destination_fund_key(self, address):
        fund_key = self._explorer_fund_key_for_address(str(address or "").strip().lower())
        if not fund_key:
            return ""
        if fund_key not in {"exchange_fund", "promo_fund", "burn"}:
            raise ValueError("official Treasury can only transfer to exchange, promo, or burn system funds")
        return fund_key

    def _explorer_address_balance_from_ledger(self, conn, address, *, limit=25, branch_uuid=None):
        branch = str(branch_uuid or self._canonical_branch_uuid(conn) or self._main_branch_uuid())
        like = f"%{address}%"
        rows = conn.execute(
            """
            SELECT *
            FROM points_ledger
            WHERE status='confirmed'
              AND chain_branch=?
              AND (public_account_id=? OR public_metadata_json LIKE ?)
            ORDER BY id ASC
            """,
            (branch, address, like),
        ).fetchall()
        balance = 0
        frozen = 0
        recent = []
        received_count = 0
        sent_count = 0
        total_received = 0
        total_sent = 0
        fees_paid = 0
        first_seen = None
        latest_seen = None
        fund_key = self._explorer_fund_key_for_address(address)
        for row in rows:
            flow = self._ledger_wallet_flow_for_read(conn, row)
            amount = int(row["amount"] or 0)
            direction = str(row["direction"] or "")
            source_address = str(flow.get("source_wallet_address") or "")
            destination_address = str(flow.get("destination_wallet_address") or "")
            affected = False
            if direction in {"credit", "transfer_in"} and destination_address == address:
                balance += amount
                received_count += 1
                total_received += amount
                affected = True
            elif direction in {"debit", "transfer_out", "reverse"} and source_address == address:
                balance -= amount
                sent_count += 1
                total_sent += amount
                if str(row["action_type"] or "") in {"wallet_transfer_fee", "chain_acceleration_fee"}:
                    fees_paid += amount
                affected = True
            elif direction == "freeze" and source_address == address:
                balance -= amount
                frozen += amount
                affected = True
            elif direction == "unfreeze" and (source_address == address or destination_address == address):
                balance += amount
                frozen -= amount
                affected = True
            elif row["public_account_id"] == address and not source_address and not destination_address:
                if direction in {"credit", "transfer_in"}:
                    balance += amount
                    received_count += 1
                    total_received += amount
                    affected = True
                elif direction in {"debit", "transfer_out", "reverse"}:
                    balance -= amount
                    sent_count += 1
                    total_sent += amount
                    affected = True
            if not affected:
                continue
            public_tx = self._explorer_public_ledger(conn, row)
            recent.append(public_tx)
            first_seen = first_seen or public_tx
            latest_seen = public_tx
        if fund_key:
            economy_rows = conn.execute(
                """
                SELECT *
                FROM points_economy_events
                WHERE status='confirmed'
                  AND chain_branch=?
                  AND (source_address=? OR destination_address=?)
                ORDER BY id ASC
                """,
                (branch, address, address),
            ).fetchall()
            for row in economy_rows:
                amount = int(row["amount"] or 0)
                source_address = str(row["source_address"] or "")
                destination_address = str(row["destination_address"] or "")
                affected = False
                if destination_address == address:
                    balance += amount
                    received_count += 1
                    total_received += amount
                    affected = True
                if source_address == address:
                    if str(row["source_fund_key"] or "") != "mint":
                        balance -= amount
                    sent_count += 1
                    total_sent += amount
                    affected = True
                if not affected:
                    continue
                public_tx = self._explorer_public_economy_event(conn, row)
                recent.append(public_tx)
                first_seen = first_seen or public_tx
                latest_seen = public_tx
        request_rows = conn.execute(
            """
            SELECT *
            FROM points_chain_transfer_requests
            WHERE status='confirmed'
              AND chain_branch=?
              AND destination_wallet_address=?
              AND (transfer_in_ledger_uuid IS NULL OR transfer_in_ledger_uuid='')
            ORDER BY id ASC
            """,
            (branch, address),
        ).fetchall()
        for req in request_rows:
            amount = int(req["amount_points"] or 0)
            balance += amount
            received_count += 1
            total_received += amount
            public_tx = self._transfer_request_public_payload(conn, req)
            recent.append(public_tx)
            first_seen = first_seen or public_tx
            latest_seen = public_tx
        pending_outgoing = self._pending_transfer_outgoing_for_address(conn, address, branch_uuid=branch)
        if pending_outgoing > 0:
            balance -= pending_outgoing
            frozen += pending_outgoing
        return {
            "points_balance": balance,
            "chain_branch": branch,
            "branch": self._branch_metadata(conn, branch),
            "points_frozen": max(0, frozen),
            "pending_outgoing_points": pending_outgoing,
            "transaction_count": len(recent),
            "received_tx_count": received_count,
            "sent_tx_count": sent_count,
            "total_received_points": total_received,
            "total_sent_points": total_sent,
            "fees_paid_points": fees_paid,
            "first_transaction": first_seen,
            "latest_transaction": latest_seen,
            "token_holdings": [
                {
                    "token": "POINTS",
                    "name": "PointsChain Points",
                    "balance": balance,
                    "frozen": max(0, frozen),
                    "pending_outgoing": pending_outgoing,
                }
            ],
            "recent_transactions": list(reversed(recent[-min(100, max(1, int(limit or 25))):])),
        }

    def explorer_wallet(self, address, *, limit=25):
        address = str(address or "").strip().lower()
        fund_key = self._explorer_fund_key_for_address(address)
        legacy_account = bool(re.fullmatch(r"[a-f0-9]{64}", address)) and not fund_key
        if not fund_key and not legacy_account and not WALLET_ADDRESS_RE.fullmatch(address):
            raise ValueError("wallet address format is invalid")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            identity = conn.execute(
                """
                SELECT address, wallet_type, custody_mode, key_algorithm, status, label, created_at
                FROM points_wallet_identities
                WHERE address=?
                LIMIT 1
                """,
                (address,),
            ).fetchone()
            balance_payload = self._explorer_address_balance_from_ledger(conn, address, limit=limit)
            if fund_key:
                report = economy_layer_report(
                    conn,
                    chain_secret=self.chain_secret,
                    actor={"role": "system", "id": None},
                    chain_branch=balance_payload.get("chain_branch") or self._canonical_branch_uuid(conn),
                )
                fund = (report.get("funds") or {}).get(fund_key) or {}
                if fund_key == "mint":
                    supply = report.get("supply") or {}
                    balance_payload["points_balance"] = int(supply.get("mint_remaining") or 0)
                else:
                    balance_payload["points_balance"] = int(fund.get("balance") or 0)
                balance_payload["points_frozen"] = 0
            risk_label = self._address_risk_label_locked(conn, address) if not legacy_account else None
            freeze = self._address_freeze_locked(conn, address) if not legacy_account else None
            provisional_freeze = self._address_provisional_freeze_locked(conn, address) if not legacy_account else None
            inner_address = bool(is_pc0_internal_address(address) and not fund_key and not legacy_account)
            address_type = "system_fund" if fund_key else ("legacy_account" if legacy_account else ("inner_address" if inner_address else "wallet"))
            wallet_type = fund_key or ("legacy_account" if legacy_account else (identity["wallet_type"] if identity else "address"))
            custody_mode = "system" if fund_key else (identity["custody_mode"] if identity else ("internal_custody" if inner_address else ""))
            status = "active" if fund_key else (identity["status"] if identity else "")
            finality_rule = (
                self._explorer_internal_hot_wallet_finality()
                if inner_address
                else self._explorer_finality_estimate(0, conn=conn)
            )
            return {
                "kind": "wallet",
                "wallet": {
                    "address": address,
                    "legacy_account": legacy_account,
                    "fund_key": fund_key,
                    "label": fund_key.replace("_", " ").title() if fund_key else (identity["label"] if identity else ("inner address" if inner_address else "")),
                    "address_type": address_type,
                    "wallet_type": wallet_type,
                    "custody_mode": custody_mode,
                    "status": status,
                    "chain_branch": balance_payload.get("chain_branch") or self._canonical_branch_uuid(conn),
                    "branch": balance_payload.get("branch") or {},
                    "points_balance": balance_payload["points_balance"],
                    "points_frozen": balance_payload["points_frozen"],
                    "pending_outgoing_points": balance_payload.get("pending_outgoing_points", 0),
                    "transaction_count": balance_payload["transaction_count"],
                    "received_tx_count": balance_payload["received_tx_count"],
                    "sent_tx_count": balance_payload["sent_tx_count"],
                    "total_received_points": balance_payload["total_received_points"],
                    "total_sent_points": balance_payload["total_sent_points"],
                    "fees_paid_points": balance_payload["fees_paid_points"],
                    "token_holdings": balance_payload["token_holdings"],
                    "first_transaction": balance_payload["first_transaction"],
                    "latest_transaction": balance_payload["latest_transaction"],
                    "recent_transactions": balance_payload["recent_transactions"],
                    "finality_rule": finality_rule,
                    "human_rule": finality_rule.get("human_rule") or EXPLORER_CHAIN_HUMAN_RULE,
                    "risk_label": risk_label,
                    "governance_freeze": freeze or provisional_freeze,
                    "provisional_freeze": provisional_freeze,
                },
            }
        finally:
            conn.close()

    def _explorer_block_payload(self, conn, block):
        rows = conn.execute(
            """
            SELECT *
            FROM points_ledger
            WHERE chain_block_id=?
            ORDER BY id ASC
            LIMIT 100
            """,
            (block["id"],),
        ).fetchall()
        transactions = [self._explorer_public_ledger(conn, row) for row in rows]
        total_fees = sum(
            int(tx.get("amount") or 0)
            for tx in transactions
            if str(tx.get("action_type") or "") in {"wallet_transfer_fee", "chain_acceleration_fee"}
        )
        gas_used = int(block["ledger_count"] or 0) * 21_000
        gas_limit = max(21_000, gas_used + 21_000)
        signatures = [
            dict(row)
            for row in conn.execute(
                """
                SELECT node_id, signature_algorithm, public_key_fingerprint, signature, signed_at
                FROM points_chain_block_signatures
                WHERE block_id=?
                ORDER BY id ASC
                """,
                (block["id"],),
            ).fetchall()
        ]
        return {
            "block_number": int(block["block_number"]),
            "block_height": int(block["block_number"]),
            "block_hash": block["block_hash"],
            "previous_block_hash": block["previous_block_hash"],
            "merkle_root": block["merkle_root"],
            "ledger_count": int(block["ledger_count"]),
            "transaction_count": int(block["ledger_count"]),
            "first_ledger_id": int(block["first_ledger_id"]),
            "last_ledger_id": int(block["last_ledger_id"]),
            "sealed_at": block["sealed_at"],
            "timestamp": block["sealed_at"],
            "seal_status": block["seal_status"],
            "anchor_status": block["anchor_status"],
            "fee_recipient": economy_fund_address(self.chain_secret, "burn"),
            "gas_used": gas_used,
            "gas_limit": gas_limit,
            "gas_used_percent": round((gas_used / gas_limit) * 100, 2) if gas_limit else 0,
            "base_fee_per_gas": "1 point/proved",
            "total_transaction_fees_points": total_fees,
            "signatures": signatures,
            "transactions": transactions,
        }

    def explorer_block(self, ref):
        ref = str(ref or "").strip()
        if not ref:
            return None
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            if ref.isdigit():
                if int(ref) == 0:
                    summary = self._explorer_economy_genesis_block_summary(conn)
                    if summary["transaction_count"] <= 0:
                        return None
                    transactions = [
                        self._explorer_public_economy_event(conn, row)
                        for row in self._explorer_economy_genesis_rows(conn)
                    ]
                    return {
                        "kind": "block",
                        "block": {
                            **summary,
                            "fee_recipient": economy_fund_address(self.chain_secret, "burn"),
                            "gas_used": len(transactions) * 21_000,
                            "gas_limit": max(21_000, len(transactions) * 21_000 + 21_000),
                            "gas_used_percent": round((len(transactions) * 21_000 / max(21_000, len(transactions) * 21_000 + 21_000)) * 100, 2),
                            "base_fee_per_gas": "0 points/system genesis",
                            "total_transaction_fees_points": 0,
                            "signatures": [],
                            "transactions": transactions,
                        },
                    }
                block = conn.execute("SELECT * FROM points_chain_blocks WHERE block_number=?", (int(ref),)).fetchone()
            else:
                block = conn.execute("SELECT * FROM points_chain_blocks WHERE block_hash=?", (ref,)).fetchone()
                if not block:
                    summary = self._explorer_economy_genesis_block_summary(conn)
                    if summary["transaction_count"] > 0 and ref == summary["block_hash"]:
                        transactions = [
                            self._explorer_public_economy_event(conn, row)
                            for row in self._explorer_economy_genesis_rows(conn)
                        ]
                        return {
                            "kind": "block",
                            "block": {
                                **summary,
                                "fee_recipient": economy_fund_address(self.chain_secret, "burn"),
                                "gas_used": len(transactions) * 21_000,
                                "gas_limit": max(21_000, len(transactions) * 21_000 + 21_000),
                                "gas_used_percent": round((len(transactions) * 21_000 / max(21_000, len(transactions) * 21_000 + 21_000)) * 100, 2),
                                "base_fee_per_gas": "0 points/system genesis",
                                "total_transaction_fees_points": 0,
                                "signatures": [],
                                "transactions": transactions,
                            },
                        }
            if not block:
                return None
            return {"kind": "block", "block": self._explorer_block_payload(conn, block)}
        finally:
            conn.close()

    def explorer_lookup(self, query, *, limit=25):
        query = str(query or "").strip()
        if not query:
            raise ValueError("query required")
        tx = self.explorer_transaction(query)
        if tx:
            return tx
        block = self.explorer_block(query)
        if block:
            return block
        bridge = self.explorer_bridge_event(query)
        if bridge:
            return bridge
        normalized = query.lower()
        if self._explorer_fund_key_for_address(normalized) or WALLET_ADDRESS_RE.fullmatch(normalized) or re.fullmatch(r"[a-f0-9]{64}", normalized):
            return self.explorer_wallet(normalized, limit=limit)
        return None

    def explorer_fee_estimate(self, *, fee_points=0):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            return self._explorer_finality_estimate(fee_points, conn=conn)
        finally:
            conn.close()

    def _explorer_actor_can_accelerate(self, conn, *, actor, ledger):
        role = actor_value(actor, "role", "user")
        username = actor_value(actor, "username", "")
        if username == "root" or role in {"manager", "super_admin"}:
            return True
        actor_id = int(actor_value(actor, "id") or 0)
        if actor_id and int(ledger["user_id"]) == actor_id:
            return True
        flow = self._ledger_wallet_flow_for_read(conn, ledger)
        addresses = {self._public_account_id(actor_id)}
        state = self._wallet_identity_state_for_user(conn, actor_id)
        addresses.update(state.get("addresses") or set())
        return any(str(flow.get(key) or "") in addresses for key in ("source_wallet_address", "destination_wallet_address", "target_wallet_address"))

    def _transfer_request_actor_can_accelerate(self, conn, *, actor, req):
        role = actor_value(actor, "role", "user")
        username = actor_value(actor, "username", "")
        if username == "root" or role in {"manager", "super_admin"}:
            return True
        actor_id = int(actor_value(actor, "id") or 0)
        if actor_id <= 0:
            return False
        if int(req["sender_user_id"] or 0) == actor_id or self._transfer_request_recipient_user_id(req) == actor_id:
            return True
        state = self._wallet_identity_state_for_user(conn, actor_id)
        addresses = {str(item or "").strip().lower() for item in (state.get("addresses") or set()) if item}
        return (
            str(req["source_wallet_address"] or "").strip().lower() in addresses
            or str(req["destination_wallet_address"] or "").strip().lower() in addresses
        )

    def accelerate_explorer_transaction(self, *, actor, ledger_ref, fee_points, request_uuid=None):
        actor_id = int(actor_value(actor, "id") or 0)
        if actor_id <= 0:
            raise PermissionError("login required")
        fee = int(fee_points or 0)
        if fee <= 0:
            raise ValueError("fee_points must be positive")
        if fee > EXPLORER_MAX_ACCELERATION_FEE_POINTS:
            raise ValueError(f"fee_points must be <= {EXPLORER_MAX_ACCELERATION_FEE_POINTS}")
        request_uuid = str(request_uuid or uuid.uuid4()).strip()[:120]
        if not request_uuid:
            raise ValueError("request_uuid required")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            ledger = self._explorer_find_ledger(conn, ledger_ref)
            transfer_req = None
            if not ledger:
                transfer_req = self._explorer_find_transfer_request(conn, ledger_ref)
            if not ledger and not transfer_req:
                raise ValueError("ledger not found")
            if transfer_req and str(transfer_req["status"] or "") != "pending":
                raise ValueError("only pending transfer requests can be accelerated")
            active_branch = self._canonical_branch_uuid(conn)
            target_branch = str(
                (ledger["chain_branch"] if ledger and "chain_branch" in ledger.keys() else "")
                or (transfer_req["chain_branch"] if transfer_req and "chain_branch" in transfer_req.keys() else "")
                or self._main_branch_uuid()
            )
            if target_branch != active_branch:
                raise PermissionError("cannot accelerate a non-canonical branch transaction")
            fee_policy = (
                self._explorer_chain_fee_policy(ledger)
                if ledger
                else self._transfer_request_chain_fee_policy(transfer_req)
            )
            if not fee_policy["acceleration_allowed"]:
                raise ValueError(fee_policy["exemption_reason"] or "this transaction is chain-fee exempt")
            if ledger and not self._explorer_actor_can_accelerate(conn, actor=actor, ledger=ledger):
                raise PermissionError("permission denied")
            if transfer_req and not self._transfer_request_actor_can_accelerate(conn, actor=actor, req=transfer_req):
                raise PermissionError("permission denied")
            target_ref = ledger["ledger_uuid"] if ledger else transfer_req["request_uuid"]
            existing = conn.execute(
                "SELECT * FROM points_chain_acceleration_requests WHERE request_uuid=?",
                (request_uuid,),
            ).fetchone()
            if existing:
                if existing["ledger_uuid"] != target_ref or int(existing["fee_points"] or 0) != fee:
                    raise ValueError("acceleration idempotency key conflict")
                conn.commit()
                if ledger:
                    refreshed = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (ledger["ledger_uuid"],)).fetchone()
                    result = {"kind": "transaction", "transaction": self._explorer_public_ledger(conn, refreshed)}
                else:
                    refreshed = conn.execute("SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?", (target_ref,)).fetchone()
                    result = {"kind": "transaction", "transaction": self._transfer_request_public_payload(conn, refreshed)}
                return {
                    "ok": True,
                    "created": False,
                    "acceleration": dict(existing),
                    "result": result,
                }
            estimate = self._explorer_finality_estimate(fee, conn=conn)
            network_state = estimate.get("network_fee_state") if isinstance(estimate, dict) else None
            fee_ledger, _created = self._record_transaction(
                conn,
                user_id=actor_id,
                currency_type=DISPLAY_CURRENCY,
                direction="debit",
                amount=fee,
                action_type="chain_acceleration_fee",
                reference_type="points_chain_explorer",
                reference_id=target_ref,
                idempotency_key=f"points_chain_acceleration:{request_uuid}",
                reason="鏈上交易加速費用",
                public_metadata={
                    "accelerated_ledger_uuid": target_ref,
                    "accelerated_ledger_hash": ledger["ledger_hash"] if ledger else transfer_req["tx_group_hash"],
                    "accelerated_transfer_request_uuid": transfer_req["request_uuid"] if transfer_req else "",
                    "target_proved_count": EXPLORER_FINALITY_PROVED_COUNT,
                },
                actor=actor,
            )
            now = utc_now()
            conn.execute(
                """
                INSERT INTO points_chain_acceleration_requests (
                    request_uuid, ledger_uuid, payer_user_id, fee_points, target_proved_count,
                    estimated_seconds_min, estimated_seconds_max, fee_ledger_uuid, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?)
                """,
                (
                    request_uuid,
                    target_ref,
                    actor_id,
                    fee,
                    EXPLORER_FINALITY_PROVED_COUNT,
                    estimate["estimated_seconds_min"],
                    estimate["estimated_seconds_max"],
                    fee_ledger["ledger_uuid"],
                    now,
                ),
            )
            conn.commit()
            if ledger:
                refreshed = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (ledger["ledger_uuid"],)).fetchone()
                result = {"kind": "transaction", "transaction": self._explorer_public_ledger(conn, refreshed, network_state=network_state)}
            else:
                refreshed = conn.execute("SELECT * FROM points_chain_transfer_requests WHERE request_uuid=?", (target_ref,)).fetchone()
                result = {"kind": "transaction", "transaction": self._transfer_request_public_payload(conn, refreshed, network_state=network_state)}
            created_row = conn.execute("SELECT * FROM points_chain_acceleration_requests WHERE request_uuid=?", (request_uuid,)).fetchone()
            return {
                "ok": True,
                "created": True,
                "acceleration": dict(created_row),
                "fee_ledger": self._explorer_public_ledger(conn, fee_ledger, network_state=network_state),
                "result": result,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ledger_proof(self, ledger_uuid):
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            ledger = conn.execute("SELECT * FROM points_ledger WHERE ledger_uuid=?", (ledger_uuid,)).fetchone()
            if not ledger:
                return None
            if not ledger["chain_block_id"]:
                return {"sealed": False, "ledger": self._serialize_ledger_for_read(conn, ledger), "ledger_hash": ledger["ledger_hash"]}
            block = conn.execute("SELECT * FROM points_chain_blocks WHERE id=?", (ledger["chain_block_id"],)).fetchone()
            rows = conn.execute(
                "SELECT id, ledger_hash FROM points_ledger WHERE chain_block_id=? ORDER BY id ASC",
                (block["id"],),
            ).fetchall()
            hashes = [row["ledger_hash"] for row in rows]
            ids = [row["id"] for row in rows]
            index = ids.index(ledger["id"])
            return {
                "sealed": True,
                "ledger_uuid": ledger["ledger_uuid"],
                "public_account_id": ledger["public_account_id"],
                "wallet_flow": self._ledger_wallet_flow_for_read(conn, ledger),
                "ledger_hash": ledger["ledger_hash"],
                "block_number": block["block_number"],
                "merkle_root": block["merkle_root"],
                "merkle_path": merkle_proof(hashes, index),
                "block_hash": block["block_hash"],
            }
        finally:
            conn.close()

    def economy_stats(self, *, verification=None):
        chain = verification
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            self._reconcile_confirmed_deposit_transfers_locked(
                conn,
                actor={"role": "system", "id": None},
                limit=1000,
            )
            conn.commit()
            if chain is None:
                chain = self.verify_chain()
            active_branch = self._canonical_branch_uuid(conn)
            wallet = conn.execute(
                """
                SELECT COALESCE(SUM(soft_balance + hard_balance), 0) AS points_balance,
                       COALESCE(SUM(soft_frozen + hard_frozen), 0) AS points_frozen,
                       COUNT(*) AS wallets
                FROM points_wallets
                """
            ).fetchone()
            ledger = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN direction IN ('credit','transfer_in') THEN amount ELSE 0 END), 0) AS points_issued,
                    COALESCE(SUM(CASE WHEN direction IN ('debit','transfer_out','reverse') THEN amount ELSE 0 END), 0) AS points_spent,
                    COUNT(*) AS ledger_entries
                FROM points_ledger
                WHERE status='confirmed'
                """
            ).fetchone()
            wallet_data = dict(wallet)
            ledger_data = dict(ledger)
            wallet_balance = int(wallet_data.get("points_balance") or 0)
            wallet_frozen = int(wallet_data.get("points_frozen") or 0)
            ledger_issued = int(ledger_data.get("points_issued") or 0)
            ledger_spent = int(ledger_data.get("points_spent") or 0)
            ledger_net = ledger_issued - ledger_spent
            raw_ledger_issued = ledger_issued
            raw_ledger_spent = ledger_spent
            raw_ledger_net = ledger_net
            ledger_data["ledger_net_points"] = ledger_net

            user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            member_wallet_balance = wallet_balance
            member_wallet_frozen = wallet_frozen
            member_wallet_count = int(wallet_data.get("wallets") or 0)
            member_ledger_issued = ledger_issued
            member_ledger_spent = ledger_spent
            raw_member_ledger_issued = member_ledger_issued
            raw_member_ledger_spent = member_ledger_spent
            if "username" in user_cols:
                member_wallet = conn.execute(
                    """
                    SELECT COALESCE(SUM(w.soft_balance + w.hard_balance), 0) AS points_balance,
                           COALESCE(SUM(w.soft_frozen + w.hard_frozen), 0) AS points_frozen,
                           COUNT(*) AS wallets
                    FROM points_wallets w
                    LEFT JOIN users u ON u.id=w.user_id
                    WHERE COALESCE(LOWER(u.username), '') != 'root'
                    """
                ).fetchone()
                member_ledger = conn.execute(
                    """
                    SELECT
                        COALESCE(SUM(CASE WHEN l.direction IN ('credit','transfer_in') THEN l.amount ELSE 0 END), 0) AS points_issued,
                        COALESCE(SUM(CASE WHEN l.direction IN ('debit','transfer_out','reverse') THEN l.amount ELSE 0 END), 0) AS points_spent,
                        COUNT(*) AS ledger_entries
                    FROM points_ledger l
                    LEFT JOIN users u ON u.id=l.user_id
                    WHERE l.status='confirmed'
                      AND COALESCE(LOWER(u.username), '') != 'root'
                    """
                ).fetchone()
                member_wallet_balance = int(member_wallet["points_balance"] or 0)
                member_wallet_frozen = int(member_wallet["points_frozen"] or 0)
                member_wallet_count = int(member_wallet["wallets"] or 0)
                member_ledger_issued = int(member_ledger["points_issued"] or 0)
                member_ledger_spent = int(member_ledger["points_spent"] or 0)
                raw_member_ledger_issued = member_ledger_issued
                raw_member_ledger_spent = member_ledger_spent

            identity_circulation = self._official_hot_wallet_circulation_with_economy_breakdown_locked(
                conn,
                branch_uuid=active_branch,
                actor={"role": "system", "id": None},
            )
            if int(identity_circulation.get("wallet_count") or 0) > 0:
                wallet_balance = int(identity_circulation.get("available_points") or 0)
                wallet_frozen = int(identity_circulation.get("frozen_points") or 0)
                member_wallet_balance = int(identity_circulation.get("member_available_points") or 0)
                member_wallet_frozen = int(identity_circulation.get("member_frozen_points") or 0)
                member_wallet_count = int(identity_circulation.get("member_wallet_count") or 0)
                wallet_data["points_balance"] = wallet_balance
                wallet_data["points_frozen"] = wallet_frozen
                wallet_data["wallets"] = int(identity_circulation.get("wallet_count") or 0)
                wallet_data["source"] = "official_hot_wallet_replay"
                ledger_issued = wallet_balance + wallet_frozen
                ledger_spent = 0
                ledger_net = ledger_issued
                member_ledger_issued = member_wallet_balance + member_wallet_frozen
                member_ledger_spent = 0
                ledger_data["ledger_net_points"] = ledger_net
                ledger_data["raw_points_issued"] = raw_ledger_issued
                ledger_data["raw_points_spent"] = raw_ledger_spent
                ledger_data["raw_ledger_net_points"] = raw_ledger_net
                ledger_data["circulation_net_source"] = "official_hot_wallet_replay"

            sealed = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN chain_block_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS sealed_ledger_entries,
                    COALESCE(SUM(CASE WHEN chain_block_id IS NULL THEN 1 ELSE 0 END), 0) AS unsealed_ledger_entries,
                    COUNT(*) AS confirmed_ledger_entries
                FROM points_ledger
                WHERE status='confirmed'
                """
            ).fetchone()
            latest = conn.execute(
                """
                SELECT
                    (SELECT created_at FROM points_ledger ORDER BY id DESC LIMIT 1) AS latest_ledger_at,
                    (SELECT updated_at FROM points_wallets ORDER BY updated_at DESC LIMIT 1) AS latest_wallet_at
                """
            ).fetchone()
            outstanding = wallet_balance + wallet_frozen
            member_outstanding = member_wallet_balance + member_wallet_frozen
            member_ledger_net = member_ledger_issued - member_ledger_spent
            confirmed_count = int(sealed["confirmed_ledger_entries"] or 0)
            sealed_count = int(sealed["sealed_ledger_entries"] or 0)
            circulation = {
                "available_points": wallet_balance,
                "frozen_points": wallet_frozen,
                "outstanding_points": outstanding,
                "member_available_points": member_wallet_balance,
                "member_frozen_points": member_wallet_frozen,
                "member_outstanding_points": member_outstanding,
                "root_outstanding_points": outstanding - member_outstanding,
                "confirmed_issued_points": ledger_issued,
                "confirmed_spent_points": ledger_spent,
                "ledger_net_points": ledger_net,
                "member_confirmed_issued_points": member_ledger_issued,
                "member_confirmed_spent_points": member_ledger_spent,
                "member_ledger_net_points": member_ledger_net,
                "supply_gap_points": outstanding - ledger_net,
                "member_supply_gap_points": member_outstanding - member_ledger_net,
                "raw_confirmed_issued_points": raw_ledger_issued,
                "raw_confirmed_spent_points": raw_ledger_spent,
                "raw_ledger_net_points": raw_ledger_net,
                "raw_member_confirmed_issued_points": raw_member_ledger_issued,
                "raw_member_confirmed_spent_points": raw_member_ledger_spent,
                "raw_member_ledger_net_points": raw_member_ledger_issued - raw_member_ledger_spent,
                "raw_supply_gap_points": outstanding - raw_ledger_net,
                "wallet_count": int(wallet_data.get("wallets") or 0),
                "member_wallet_count": member_wallet_count,
                "root_wallet_count": max(0, int(wallet_data.get("wallets") or 0) - member_wallet_count),
                "confirmed_ledger_entries": confirmed_count,
                "sealed_ledger_entries": sealed_count,
                "unsealed_ledger_entries": int(sealed["unsealed_ledger_entries"] or 0),
                "sealed_coverage_percent": round((sealed_count / confirmed_count) * 100, 4) if confirmed_count else 0,
                "latest_ledger_at": latest["latest_ledger_at"] if latest else None,
                "latest_wallet_at": latest["latest_wallet_at"] if latest else None,
            }
            for key in (
                "economy_external_balance_breakdown",
                "bridge_flow_totals",
                "economy_pc0_member_internal_points",
                "economy_pc0_root_internal_points",
                "economy_pc0_unknown_internal_points",
                "economy_pc0_internal_points",
                "margin_settlement_withheld_breakdown",
                "exchange_margin_settlement_withheld_contra_points",
                "economy_pc0_member_reconciled_points",
                "economy_pc0_root_reconciled_points",
                "economy_pc0_unknown_reconciled_points",
                "economy_pc0_reconciled_points",
                "off_wallet_economy_external_points",
                "ledger_vs_economy_external_gap_points",
                "member_ledger_vs_economy_external_gap_points",
                "root_ledger_vs_economy_external_gap_points",
            ):
                if key in identity_circulation:
                    circulation[key] = identity_circulation[key]
            economy_layer = economy_layer_report(
                conn,
                chain_secret=self.chain_secret,
                actor={"role": "system", "id": None},
                circulation=circulation,
                chain_branch=active_branch,
            )
            bridge_report = economy_layer.get("legacy_bridge", {}) if isinstance(economy_layer.get("legacy_bridge"), dict) else {}
            if (
                not bridge_report.get("bridged_supply_equation_balanced")
                or int(bridge_report.get("ledger_vs_economy_external_gap_points") or 0) != 0
            ):
                backfill = self._backfill_walletized_ledger_events(conn)
                if backfill.get("created"):
                    identity_circulation = self._official_hot_wallet_circulation_with_economy_breakdown_locked(
                        conn,
                        branch_uuid=self._canonical_branch_uuid(conn),
                        actor={"role": "system", "id": None},
                    )
                    circulation.update({
                        "economy_external_balance_breakdown": identity_circulation.get("economy_external_balance_breakdown") or {},
                        "bridge_flow_totals": identity_circulation.get("bridge_flow_totals") or {},
                        "economy_pc0_member_internal_points": identity_circulation.get("economy_pc0_member_internal_points", 0),
                        "economy_pc0_root_internal_points": identity_circulation.get("economy_pc0_root_internal_points", 0),
                        "economy_pc0_unknown_internal_points": identity_circulation.get("economy_pc0_unknown_internal_points", 0),
                        "economy_pc0_internal_points": identity_circulation.get("economy_pc0_internal_points", 0),
                        "margin_settlement_withheld_breakdown": identity_circulation.get("margin_settlement_withheld_breakdown") or {},
                        "exchange_margin_settlement_withheld_contra_points": identity_circulation.get("exchange_margin_settlement_withheld_contra_points", 0),
                        "economy_pc0_member_reconciled_points": identity_circulation.get("economy_pc0_member_reconciled_points", 0),
                        "economy_pc0_root_reconciled_points": identity_circulation.get("economy_pc0_root_reconciled_points", 0),
                        "economy_pc0_unknown_reconciled_points": identity_circulation.get("economy_pc0_unknown_reconciled_points", 0),
                        "economy_pc0_reconciled_points": identity_circulation.get("economy_pc0_reconciled_points", 0),
                        "off_wallet_economy_external_points": identity_circulation.get("off_wallet_economy_external_points", 0),
                        "ledger_vs_economy_external_gap_points": identity_circulation.get("ledger_vs_economy_external_gap_points", 0),
                        "member_ledger_vs_economy_external_gap_points": identity_circulation.get("member_ledger_vs_economy_external_gap_points", 0),
                        "root_ledger_vs_economy_external_gap_points": identity_circulation.get("root_ledger_vs_economy_external_gap_points", 0),
                    })
                    economy_layer = economy_layer_report(
                        conn,
                        chain_secret=self.chain_secret,
                        actor={"role": "system", "id": None},
                        circulation=circulation,
                        chain_branch=self._canonical_branch_uuid(conn),
                    )
                    economy_layer["walletization_backfill"] = backfill
            conn.commit()
            return {
                "wallets": wallet_data,
                "ledger": ledger_data,
                "chain": chain,
                "circulation": circulation,
                "economy_layer": economy_layer,
                "currency_type": DISPLAY_CURRENCY,
            }
        finally:
            conn.close()

    def block_schedule(self, *, ledger_threshold=DEFAULT_BLOCK_LEDGER_THRESHOLD, max_interval_seconds=DEFAULT_BLOCK_MAX_INTERVAL_SECONDS, verification=None, verify=True):
        if verification is None and verify:
            verification = self.verify_chain()
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            ledger_threshold = max(1, int(ledger_threshold or DEFAULT_BLOCK_LEDGER_THRESHOLD))
            max_interval_seconds = max(60, int(max_interval_seconds or DEFAULT_BLOCK_MAX_INTERVAL_SECONDS))
            branch = self._canonical_branch_uuid(conn)
            pc1_unsealed_rows = self._pc1_unsealed_ledger_rows_locked(conn, branch=branch, limit=1)
            first_unsealed = pc1_unsealed_rows[0] if pc1_unsealed_rows else None
            if verification is None:
                safe_mode = self._safe_mode_status(conn)
                counts = self._pc1_chain_ledger_count_snapshot_locked(conn, branch=branch)
                unsealed_count = counts["pc1_unsealed_entries"]
                verification = {
                    "ok": not bool(safe_mode.get("safe_mode")),
                    "counts": {"unsealed_entries": int(unsealed_count or 0)},
                    "safe_mode": safe_mode,
                    "verification_mode": "schedule_snapshot",
                }
            anchor_at = parse_utc_timestamp(first_unsealed["created_at"]) if first_unsealed else None
            next_at = anchor_at.timestamp() + max_interval_seconds if anchor_at else None
            now_ts = datetime.now(timezone.utc).timestamp()
            seconds_remaining = int(max(0, next_at - now_ts)) if next_at else None
            unsealed = int((verification.get("counts") or {}).get("unsealed_entries") or 0)
            chain_ok = verification.get("ok") is True
            entries_remaining = max(0, ledger_threshold - unsealed)
            count_due = unsealed >= ledger_threshold
            time_due = bool(unsealed and seconds_remaining == 0)
            due_reason = "count" if count_due else ("time" if time_due else None)
            return {
                "mode": "hybrid",
                "ledger_threshold": ledger_threshold,
                "entries_remaining": entries_remaining,
                "max_interval_seconds": max_interval_seconds,
                "max_interval_minutes": max_interval_seconds // 60,
                "interval_seconds": max_interval_seconds,
                "interval_minutes": max_interval_seconds // 60,
                "next_seal_at": datetime.fromtimestamp(next_at, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if next_at else None,
                "seconds_remaining": seconds_remaining,
                "unsealed_entries": unsealed,
                "chain_ok": chain_ok,
                "verification_mode": verification.get("verification_mode"),
                "verification_bounded": bool(verification.get("bounded")),
                "due": bool(chain_ok and (count_due or time_due)),
                "due_reason": due_reason,
                "message": "全鏈驗證異常，暫停自動封塊" if not chain_ok else ("目前沒有未封 ledger" if not unsealed else (f"已累積 {unsealed}/{ledger_threshold} 筆，可封塊" if count_due else ("已到達最長等待時間，可封塊" if time_due else f"已累積 {unsealed}/{ledger_threshold} 筆，尚需 {entries_remaining} 筆或等待時間到"))),
            }
        finally:
            conn.close()

    def _root_recent_unsealed_transactions(self, conn, *, limit=50):
        limit = max(1, min(100, int(limit or 50)))
        pending_requests = conn.execute(
            """
            SELECT *
            FROM points_chain_transfer_requests
            WHERE status='pending'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        unsealed_candidates = conn.execute(
            """
            SELECT *
            FROM points_ledger
            WHERE chain_block_id IS NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (max(limit * 20, 500),),
        ).fetchall()
        unsealed_ledgers = [
            row for row in unsealed_candidates
            if self._ledger_is_pc1_canonical_sealable(conn, row)
        ][:limit]
        rows = []
        for req in pending_requests:
            payload = self._transfer_request_public_payload(conn, req)
            rows.append({
                "source": "pending_transfer",
                "transaction_hash": payload.get("transaction_hash") or payload.get("ledger_hash") or "",
                "ledger_uuid": payload.get("ledger_uuid") or "",
                "ledger_hash": payload.get("ledger_hash") or "",
                "action_type": payload.get("action_type") or "wallet_transfer",
                "amount": int(payload.get("amount") or 0),
                "status": payload.get("status") or "pending",
                "created_at": payload.get("created_at") or "",
                "finality": payload.get("finality") or {},
                "wallet_flow": payload.get("wallet_flow") or {},
            })
        for ledger in unsealed_ledgers:
            payload = self._explorer_public_ledger(conn, ledger)
            rows.append({
                "source": "unsealed_ledger",
                "transaction_hash": payload.get("ledger_hash") or payload.get("transaction_hash") or "",
                "ledger_uuid": payload.get("ledger_uuid") or "",
                "ledger_hash": payload.get("ledger_hash") or "",
                "action_type": payload.get("action_type") or "",
                "amount": int(payload.get("amount") or 0),
                "status": payload.get("status") or "confirmed",
                "created_at": payload.get("created_at") or "",
                "finality": payload.get("finality") or {},
                "wallet_flow": payload.get("wallet_flow") or {},
            })
        rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
        return rows[:limit]

    def root_report(self):
        started = _monotonic_time.perf_counter()
        phase_started = started
        phases = []

        def mark_phase(name):
            nonlocal phase_started
            now = _monotonic_time.perf_counter()
            phases.append({"phase": name, "elapsed_ms": round((now - phase_started) * 1000, 3)})
            phase_started = now

        verification = self.verify_chain(include_financial=False)
        mark_phase("verify_chain")
        scheduled_backup = {
            "ok": True,
            "created": False,
            "disabled": True,
            "msg": "PointsChain ledger backup/restore is disabled; recovery uses safe mode, branches, governance, and append-only correction entries.",
        }
        stats = self.economy_stats(verification=verification)
        mark_phase("economy_stats")
        audit_logs = self.list_chain_audit_logs(limit=50)
        mark_phase("audit_logs")
        block_schedule = self.block_schedule(verification=verification)
        mark_phase("block_schedule")
        backups = []
        recovery = self.safe_mode_status()
        mark_phase("recovery")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            blocks = conn.execute(
                """
                SELECT b.*, s.signature_algorithm, s.public_key_fingerprint, s.signed_at
                FROM points_chain_blocks b
                LEFT JOIN points_chain_block_signatures s ON s.block_id=b.id AND s.node_id='single-node'
                ORDER BY b.block_number DESC LIMIT 10
                """
            ).fetchall()
            high_risk = conn.execute(
                """
                SELECT * FROM points_ledger
                WHERE risk_flag != 'none' OR status != 'confirmed'
                ORDER BY id DESC LIMIT 20
                """
            ).fetchall()
            high_risk_by_id = {int(row["id"]): self.serialize_ledger(row, include_user_id=False) for row in high_risk}
            for error in verification.get("errors") or []:
                ledger = error.get("ledger") if isinstance(error, dict) else None
                ledger_id = int(error.get("ledger_id") or 0) if isinstance(error, dict) else 0
                if not ledger_id:
                    continue
                if not ledger:
                    row = conn.execute("SELECT * FROM points_ledger WHERE id=?", (ledger_id,)).fetchone()
                    ledger = self.serialize_ledger(row, include_user_id=False) if row else None
                if not ledger:
                    continue
                existing = high_risk_by_id.get(ledger_id) or dict(ledger)
                issues = list(existing.get("verification_errors") or [])
                issues.append({
                    "type": error.get("type"),
                    "message": error.get("message"),
                    "expected_ledger_hash": error.get("expected_ledger_hash"),
                    "actual_ledger_hash": error.get("actual_ledger_hash"),
                    "expected_previous_ledger_hash": error.get("expected_previous_ledger_hash"),
                    "actual_previous_ledger_hash": error.get("actual_previous_ledger_hash"),
                })
                existing["verification_errors"] = issues
                existing["verification_status"] = "tampered"
                high_risk_by_id[ledger_id] = existing
            high_risk_ledger = sorted(high_risk_by_id.values(), key=lambda row: int(row.get("id") or 0), reverse=True)[:20]
            mark_phase("risk_snapshot")
            financial_invariants = self._financial_invariant_report_locked(
                conn,
                economy_layer=(stats.get("economy_layer") if isinstance(stats, dict) else None),
            )
            mark_phase("financial_invariants")
            verification["financial_invariants"] = financial_invariants
            verification["financial_ok"] = bool(financial_invariants.get("ok"))
            unsealed_transactions = self._root_recent_unsealed_transactions(conn, limit=50)
            mark_phase("unsealed_transactions")
            governance = self._governance_report_locked(conn)
            mark_phase("governance")
            report = {
                "verification": verification,
                "stats": stats,
                "financial_invariants": financial_invariants,
                "blocks": [dict(row) for row in blocks],
                "high_risk_ledger": high_risk_ledger,
                "audit_logs": audit_logs,
                "unsealed_transactions": unsealed_transactions,
                "block_schedule": block_schedule,
                "ledger_backups": backups,
                "recovery": recovery,
                "scheduled_backup": scheduled_backup,
                "governance": governance,
            }
            report["management_timing"] = {
                "total_ms": round((_monotonic_time.perf_counter() - started) * 1000, 3),
                "phases": phases,
            }
            return report
        finally:
            conn.close()

    def root_report_bounded_snapshot(self, *, recent_limit=1000):
        started = _monotonic_time.perf_counter()
        phase_started = started
        phases = []

        def mark_phase(name):
            nonlocal phase_started
            now = _monotonic_time.perf_counter()
            phases.append({"phase": name, "elapsed_ms": round((now - phase_started) * 1000, 3)})
            phase_started = now

        verification = self.verify_chain_bounded_snapshot(recent_limit=recent_limit)
        mark_phase("bounded_verify")
        audit_logs = self.list_chain_audit_logs(limit=50)
        mark_phase("audit_logs")
        block_schedule = self.block_schedule(verification=verification, verify=False)
        mark_phase("block_schedule")
        recovery = self.safe_mode_status()
        mark_phase("recovery")
        conn = self.get_db()
        try:
            self.ensure_schema(conn)
            branch = self._canonical_branch_uuid(conn)
            wallet = conn.execute(
                """
                SELECT COALESCE(SUM(soft_balance + hard_balance), 0) AS points_balance,
                       COALESCE(SUM(soft_frozen + hard_frozen), 0) AS points_frozen,
                       COUNT(*) AS wallets
                FROM points_wallets
                """
            ).fetchone()
            ledger = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN direction IN ('credit','transfer_in') THEN amount ELSE 0 END), 0) AS points_issued,
                    COALESCE(SUM(CASE WHEN direction IN ('debit','transfer_out','reverse') THEN amount ELSE 0 END), 0) AS points_spent,
                    COUNT(*) AS ledger_entries
                FROM points_ledger
                WHERE status='confirmed'
                """
            ).fetchone()
            sealed = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN chain_block_id IS NOT NULL THEN 1 ELSE 0 END), 0) AS sealed_ledger_entries,
                    COALESCE(SUM(CASE WHEN chain_block_id IS NULL THEN 1 ELSE 0 END), 0) AS unsealed_ledger_entries,
                    COUNT(*) AS confirmed_ledger_entries
                FROM points_ledger
                WHERE status='confirmed'
                """
            ).fetchone()
            latest = conn.execute(
                """
                SELECT
                    (SELECT created_at FROM points_ledger ORDER BY id DESC LIMIT 1) AS latest_ledger_at,
                    (SELECT updated_at FROM points_wallets ORDER BY updated_at DESC LIMIT 1) AS latest_wallet_at
                """
            ).fetchone()
            blocks = conn.execute(
                """
                SELECT b.*, s.signature_algorithm, s.public_key_fingerprint, s.signed_at
                FROM points_chain_blocks b
                LEFT JOIN points_chain_block_signatures s ON s.block_id=b.id AND s.node_id='single-node'
                ORDER BY b.block_number DESC LIMIT 10
                """
            ).fetchall()
            high_risk = conn.execute(
                """
                SELECT * FROM points_ledger
                WHERE risk_flag != 'none' OR status != 'confirmed'
                ORDER BY id DESC LIMIT 20
                """
            ).fetchall()
            wallet_data = dict(wallet)
            ledger_data = dict(ledger)
            ledger_data["ledger_net_points"] = int(ledger_data.get("points_issued") or 0) - int(ledger_data.get("points_spent") or 0)
            confirmed_count = int(sealed["confirmed_ledger_entries"] or 0)
            sealed_count = int(sealed["sealed_ledger_entries"] or 0)
            stats = {
                "wallets": wallet_data,
                "ledger": ledger_data,
                "chain": verification,
                "circulation": {
                    "available_points": int(wallet_data.get("points_balance") or 0),
                    "frozen_points": int(wallet_data.get("points_frozen") or 0),
                    "outstanding_points": int(wallet_data.get("points_balance") or 0) + int(wallet_data.get("points_frozen") or 0),
                    "wallet_count": int(wallet_data.get("wallets") or 0),
                    "confirmed_ledger_entries": confirmed_count,
                    "sealed_ledger_entries": sealed_count,
                    "unsealed_ledger_entries": int(sealed["unsealed_ledger_entries"] or 0),
                    "sealed_coverage_percent": round((sealed_count / confirmed_count) * 100, 4) if confirmed_count else 0,
                    "latest_ledger_at": latest["latest_ledger_at"] if latest else None,
                    "latest_wallet_at": latest["latest_wallet_at"] if latest else None,
                    "bounded": True,
                },
                "currency_type": DISPLAY_CURRENCY,
                "bounded": True,
            }
            mark_phase("bounded_stats")
            financial_invariants = {
                "ok": None,
                "status": "snapshot_only",
                "bounded": True,
                "chain_branch": branch,
                "msg": "Full financial invariant replay is deferred to offline/full audit; this snapshot stays bounded for root UI.",
            }
            unsealed_rows = conn.execute(
                """
                SELECT *
                FROM points_ledger
                WHERE status='confirmed'
                  AND chain_block_id IS NULL
                  AND chain_branch=?
                ORDER BY id DESC
                LIMIT 50
                """,
                (branch,),
            ).fetchall()
            unsealed_transactions = [
                {
                    **self.serialize_ledger(row, include_user_id=False),
                    "bounded_snapshot": True,
                    "pc1_pc0_classification": "deferred",
                }
                for row in unsealed_rows
            ]
            mark_phase("unsealed_transactions")
            report = {
                "verification": verification,
                "stats": stats,
                "financial_invariants": financial_invariants,
                "blocks": [dict(row) for row in blocks],
                "high_risk_ledger": [self.serialize_ledger(row, include_user_id=False) for row in high_risk],
                "audit_logs": audit_logs,
                "unsealed_transactions": unsealed_transactions,
                "block_schedule": block_schedule,
                "ledger_backups": [],
                "recovery": recovery,
                "scheduled_backup": {
                    "ok": True,
                    "created": False,
                    "disabled": True,
                    "msg": "PointsChain ledger backup/restore is disabled; recovery uses safe mode, branches, governance, and append-only correction entries.",
                },
                "governance": {
                    "bounded": True,
                    "msg": "Full governance aggregation is deferred to offline/full audit.",
                },
                "bounded": True,
            }
            report["management_timing"] = {
                "total_ms": round((_monotonic_time.perf_counter() - started) * 1000, 3),
                "phases": phases,
                "bounded": True,
            }
            return report
        finally:
            conn.close()


from . import backup_recovery as _backup_recovery

for _name in ('_backup_payload', '_chain_head_summary', '_verify_backup_payload', '_write_json_private', 'create_ledger_backup', '_create_ledger_backup', '_load_backup_from_catalog', '_healthy_backups', 'list_ledger_backups', '_prune_ledger_backups', '_scheduled_backup_due', 'create_scheduled_backup_if_due', '_create_forensic_bundle', '_build_restore_plan', '_enter_safe_mode'):
    setattr(PointsLedgerService, _name, getattr(_backup_recovery, _name))

del _backup_recovery
del _name
