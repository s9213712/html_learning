"""Replayable private economy layer for PointsChain Phase 1A.

This module is intentionally separate from product reward / trading flows. It
creates the fund-wallet and policy foundation, then derives balances from an
append-only economy event ledger.
"""

from __future__ import annotations

import json
import uuid

from .schema import canonical_json, sha256_text, utc_now
from .wallet_identity import BURN_WALLET_ADDRESS, LEGACY_PC0_BURN_WALLET_ADDRESS, internal_address_from_hash, system_wallet_address


ECONOMY_POLICY_VERSION = "phase1_sim_economy_v1"
ECONOMY_FUND_KEYS = {"mint", "burn", "official_treasury", "promo_fund", "exchange_fund"}
ECONOMY_EVENT_STATUSES = {"confirmed"}
ECONOMY_INCIDENT_STATUSES = {"open", "resolved", "acknowledged"}
ECONOMY_INCIDENT_SEVERITIES = {"info", "warning", "critical"}
EXCHANGE_PRINCIPAL_LENT_TYPES = {
    "margin_principal_lent",
    "margin_collateral_withdraw_principal_lent",
}
EXCHANGE_PRINCIPAL_REPAID_TYPES = {"margin_principal_repaid"}
EXCHANGE_PRINCIPAL_RECEIVABLE_ADDRESS_PREFIX = "exchange_margin_receivable:"


def exchange_principal_receivable_address(source_user_id):
    try:
        user_key = str(int(source_user_id))
    except (TypeError, ValueError):
        user_key = "unassigned"
    return f"{EXCHANGE_PRINCIPAL_RECEIVABLE_ADDRESS_PREFIX}user:{user_key}"

DEFAULT_ECONOMY_POLICY = {
    "policy_version": ECONOMY_POLICY_VERSION,
    "max_supply": 100_000_000,
    "reserved_locked": 40_000_000,
    "initial_mint": 20_000_000,
    "official_treasury_initial": 10_000_000,
    "promo_fund_initial": 5_000_000,
    "exchange_fund_initial": 5_000_000,
    "promo_daily_cap": 50_000,
    "promo_user_daily_cap": 1_000,
    "promo_action_daily_cap": 20_000,
    "promo_low_watermark": 500_000,
    "promo_critical_watermark": 100_000,
    "exchange_low_watermark": 1_000_000,
    "exchange_critical_watermark": 250_000,
    "mint_replenish_max_once": 1_000_000,
    "mint_replenish_min_remaining": 50_000_000,
    "dev_official_transfer_single_root_signature": True,
    "phase": "1A",
}

DEFAULT_FUND_LABELS = {
    "mint": "MINT 發行錢包",
    "burn": "BURN 銷毀錢包",
    "official_treasury": "官方 Treasury 錢包",
    "promo_fund": "PROMO 獎勵基金",
    "exchange_fund": "EXCHANGE 交易所基金",
}


def _sql_in(values):
    return ", ".join(f"'{value}'" for value in sorted(values))


def _json_loads(raw, fallback=None):
    if not raw:
        return fallback if fallback is not None else {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, (dict, list)) else (fallback if fallback is not None else {})
    except Exception:
        return fallback if fallback is not None else {}


def _json_dumps(value):
    return canonical_json(value if value is not None else {})


def economy_fund_address(chain_secret, fund_key):
    fund_key = str(fund_key or "").strip().lower()
    if fund_key in {"mint", "burn"}:
        return system_wallet_address(chain_secret, fund_key)
    if fund_key not in ECONOMY_FUND_KEYS:
        raise ValueError("unsupported economy fund key")
    return internal_address_from_hash(f"economy_fund:{fund_key}:{chain_secret or ''}")


def _is_burn_address(address, *, wallets=None):
    address = str(address or "").strip()
    if not address:
        return False
    burn_addresses = {BURN_WALLET_ADDRESS, LEGACY_PC0_BURN_WALLET_ADDRESS}
    if wallets and "burn" in wallets:
        burn_addresses.add(str(wallets["burn"]["address"] or "").strip())
    return address in burn_addresses


def _policy_original_max_supply(policy):
    metadata = policy if isinstance(policy, dict) else {}
    return int(
        metadata.get("constitutional_original_max_supply")
        or metadata.get("original_max_supply")
        or DEFAULT_ECONOMY_POLICY["max_supply"]
        or metadata.get("max_supply")
        or 0
    )


def _policy_supply_expansion_restriction(policy):
    metadata = policy if isinstance(policy, dict) else {}
    latest = metadata.get("latest_supply_expansion")
    if isinstance(latest, dict):
        return latest
    restrictions = metadata.get("supply_expansion_restrictions")
    if isinstance(restrictions, list) and restrictions:
        latest = restrictions[-1]
        return latest if isinstance(latest, dict) else {}
    return {}


def _expanded_supply_mint_portion(policy, replay, amount):
    original_max = _policy_original_max_supply(policy)
    if original_max <= 0:
        return 0
    original_releasable = original_max - int(policy.get("reserved_locked") or 0)
    minted_total = int((replay or {}).get("minted_total") or 0)
    before = max(0, minted_total - original_releasable)
    after = max(0, minted_total + int(amount or 0) - original_releasable)
    return max(0, after - before)


def economy_event_hash_payload(row):
    return {
        "event_uuid": row["event_uuid"],
        "event_type": row["event_type"],
        "transaction_type": row["transaction_type"],
        "source_fund_key": row["source_fund_key"],
        "source_address": row["source_address"],
        "destination_fund_key": row["destination_fund_key"],
        "destination_address": row["destination_address"],
        "amount": int(row["amount"]),
        "idempotency_key": row["idempotency_key"],
        "request_hash": row["request_hash"],
        "policy_version": row["policy_version"],
        "metadata_json": row["metadata_json"],
        "previous_event_hash": row["previous_event_hash"],
        "created_at": row["created_at"],
    }


def compute_economy_event_hash(row):
    return sha256_text(canonical_json(economy_event_hash_payload(row)))


def ensure_economy_layer_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS points_economy_policy (
            id INTEGER PRIMARY KEY CHECK (id=1),
            policy_version TEXT NOT NULL,
            max_supply INTEGER NOT NULL CHECK (max_supply > 0),
            reserved_locked INTEGER NOT NULL DEFAULT 0 CHECK (reserved_locked >= 0),
            initial_mint INTEGER NOT NULL DEFAULT 0 CHECK (initial_mint >= 0),
            policy_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS points_economy_fund_wallets (
            fund_key TEXT PRIMARY KEY CHECK (fund_key IN ({_sql_in(ECONOMY_FUND_KEYS)})),
            address TEXT NOT NULL UNIQUE,
            label TEXT NOT NULL,
            custody_mode TEXT NOT NULL DEFAULT 'system',
            derived_cache INTEGER NOT NULL DEFAULT 0 CHECK (derived_cache IN (0, 1)),
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS points_economy_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_uuid TEXT NOT NULL UNIQUE,
            chain_branch TEXT NOT NULL DEFAULT 'main',
            event_type TEXT NOT NULL,
            transaction_type TEXT NOT NULL,
            source_fund_key TEXT CHECK (source_fund_key IS NULL OR source_fund_key IN ({_sql_in(ECONOMY_FUND_KEYS)})),
            source_address TEXT,
            destination_fund_key TEXT CHECK (destination_fund_key IS NULL OR destination_fund_key IN ({_sql_in(ECONOMY_FUND_KEYS)})),
            destination_address TEXT,
            amount INTEGER NOT NULL CHECK (amount > 0),
            idempotency_key TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            policy_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ({_sql_in(ECONOMY_EVENT_STATUSES)})),
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            previous_event_hash TEXT,
            event_hash TEXT NOT NULL UNIQUE,
            created_by INTEGER,
            created_by_role TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    event_cols = {row["name"] for row in conn.execute("PRAGMA table_info(points_economy_events)").fetchall()}
    if "chain_branch" not in event_cols:
        conn.execute("ALTER TABLE points_economy_events ADD COLUMN chain_branch TEXT NOT NULL DEFAULT 'main'")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_points_economy_events_branch_id
        ON points_economy_events(chain_branch, id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS points_economy_derived_balances (
            fund_key TEXT PRIMARY KEY,
            address TEXT NOT NULL,
            balance INTEGER NOT NULL CHECK (balance >= 0),
            derived_cache INTEGER NOT NULL DEFAULT 1 CHECK (derived_cache=1),
            replay_height INTEGER NOT NULL DEFAULT 0,
            replay_event_hash TEXT,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS points_economy_incidents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_uuid TEXT NOT NULL UNIQUE,
            severity TEXT NOT NULL CHECK (severity IN ({_sql_in(ECONOMY_INCIDENT_SEVERITIES)})),
            category TEXT NOT NULL,
            trigger TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ({_sql_in(ECONOMY_INCIDENT_STATUSES)})),
            automatic_actions_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{{}}',
            created_at TEXT NOT NULL,
            resolved_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS points_economy_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_uuid TEXT NOT NULL UNIQUE,
            snapshot_height INTEGER NOT NULL,
            event_hash TEXT,
            wallet_root_hash TEXT NOT NULL,
            minted_total INTEGER NOT NULL,
            burned_total INTEGER NOT NULL,
            active_supply INTEGER NOT NULL,
            circulating_supply INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_points_economy_snapshots_replay
        ON points_economy_snapshots (snapshot_height, event_hash, wallet_root_hash)
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_points_economy_events_no_update
        BEFORE UPDATE ON points_economy_events
        BEGIN
            SELECT RAISE(ABORT, 'points economy events are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_points_economy_events_no_delete
        BEFORE DELETE ON points_economy_events
        BEGIN
            SELECT RAISE(ABORT, 'points economy events are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_points_economy_incidents_core_immutable
        BEFORE UPDATE OF incident_uuid, severity, category, trigger, automatic_actions_json,
                         metadata_json, created_at
        ON points_economy_incidents
        BEGIN
            SELECT RAISE(ABORT, 'points economy incident core fields are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_points_economy_incidents_no_update
        BEFORE UPDATE ON points_economy_incidents
        BEGIN
            SELECT RAISE(ABORT, 'points economy incidents are append-only');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS trg_points_economy_incidents_no_delete
        BEFORE DELETE ON points_economy_incidents
        BEGIN
            SELECT RAISE(ABORT, 'points economy incidents are append-only');
        END
        """
    )


def load_economy_policy(conn):
    ensure_economy_layer_schema(conn)
    row = conn.execute("SELECT * FROM points_economy_policy WHERE id=1").fetchone()
    if row:
        payload = _json_loads(row["policy_json"], {})
        payload.update({
            "policy_version": row["policy_version"],
            "max_supply": int(row["max_supply"]),
            "reserved_locked": int(row["reserved_locked"]),
            "initial_mint": int(row["initial_mint"]),
        })
        payload["releasable_supply"] = int(payload["max_supply"]) - int(payload["reserved_locked"])
        return payload
    now = utc_now()
    policy = dict(DEFAULT_ECONOMY_POLICY)
    conn.execute(
        """
        INSERT INTO points_economy_policy (
            id, policy_version, max_supply, reserved_locked, initial_mint,
            policy_json, created_at, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            policy["policy_version"],
            int(policy["max_supply"]),
            int(policy["reserved_locked"]),
            int(policy["initial_mint"]),
            _json_dumps(policy),
            now,
            now,
        ),
    )
    policy["releasable_supply"] = int(policy["max_supply"]) - int(policy["reserved_locked"])
    return policy


def ensure_economy_fund_wallets(conn, *, chain_secret):
    ensure_economy_layer_schema(conn)
    now = utc_now()
    wallets = {}
    for fund_key in ("mint", "burn", "official_treasury", "promo_fund", "exchange_fund"):
        address = economy_fund_address(chain_secret, fund_key)
        custody_mode = "system" if fund_key in {"mint", "burn"} else "server_hot"
        metadata = {
            "phase": "1A",
            "financial_source_of_truth": "points_economy_events",
            "address_namespace": "system_special" if fund_key in {"mint", "burn"} else "pc0_internal",
            "owner_type": "system" if fund_key in {"mint", "burn"} else "platform",
            "not_official_hot_wallet": fund_key in {"mint", "burn"},
        }
        row = conn.execute("SELECT * FROM points_economy_fund_wallets WHERE fund_key=?", (fund_key,)).fetchone()
        if row and (row["address"] != address or row["custody_mode"] != custody_mode):
            conn.execute(
                """
                UPDATE points_economy_fund_wallets
                SET address=?, label=?, custody_mode=?, metadata_json=?, updated_at=?
                WHERE fund_key=?
                """,
                (address, DEFAULT_FUND_LABELS[fund_key], custody_mode, _json_dumps(metadata), now, fund_key),
            )
            row = conn.execute("SELECT * FROM points_economy_fund_wallets WHERE fund_key=?", (fund_key,)).fetchone()
        elif not row:
            conn.execute(
                """
                INSERT INTO points_economy_fund_wallets (
                    fund_key, address, label, custody_mode, derived_cache,
                    metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    fund_key,
                    address,
                    DEFAULT_FUND_LABELS[fund_key],
                    custody_mode,
                    _json_dumps(metadata),
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM points_economy_fund_wallets WHERE fund_key=?", (fund_key,)).fetchone()
        wallets[fund_key] = dict(row)
    return wallets


def _last_economy_event_hash(conn, *, chain_branch="main"):
    branch = str(chain_branch or "main").strip() or "main"
    row = conn.execute(
        """
        SELECT event_hash FROM points_economy_events
        WHERE chain_branch=?
        ORDER BY id DESC LIMIT 1
        """,
        (branch,),
    ).fetchone()
    return row["event_hash"] if row else None


def _event_request_hash(payload):
    return sha256_text(canonical_json(payload))


def _legacy_branchless_event_request_hash(payload):
    legacy_payload = dict(payload)
    legacy_payload.pop("chain_branch", None)
    return _event_request_hash(legacy_payload)


def append_economy_event(
    conn,
    *,
    chain_secret,
    event_type,
    transaction_type,
    source_fund_key,
    destination_fund_key,
    source_address=None,
    destination_address=None,
    amount,
    idempotency_key,
    metadata=None,
    actor=None,
    allow_mint_override=False,
    chain_branch="main",
    skip_preflight_replay=False,
):
    policy = load_economy_policy(conn)
    wallets = ensure_economy_fund_wallets(conn, chain_secret=chain_secret)
    chain_branch = str(chain_branch or "main").strip() or "main"
    source_fund_key = str(source_fund_key or "").strip().lower() or None
    destination_fund_key = str(destination_fund_key or "").strip().lower() or None
    legacy_destination_fund_key = destination_fund_key
    if source_fund_key is not None and source_fund_key not in ECONOMY_FUND_KEYS:
        raise ValueError("source fund is unsupported")
    if destination_fund_key is not None and destination_fund_key not in ECONOMY_FUND_KEYS:
        raise ValueError("destination fund is unsupported")
    source_address = str(source_address or "").strip()
    destination_address = str(destination_address or "").strip()
    if source_fund_key == "burn" or _is_burn_address(source_address, wallets=wallets):
        raise ValueError("burn address is unspendable")
    if destination_fund_key is None and _is_burn_address(destination_address, wallets=wallets):
        destination_fund_key = "burn"
    if source_fund_key is None and not source_address:
        raise ValueError("source address is required when source fund is omitted")
    if destination_fund_key is None and not destination_address:
        raise ValueError("destination address is required when destination fund is omitted")
    amount = int(amount)
    if amount <= 0:
        raise ValueError("economy event amount must be positive")
    request_payload = {
        "event_type": str(event_type),
        "transaction_type": str(transaction_type),
        "source_fund_key": source_fund_key,
        "destination_fund_key": destination_fund_key,
        "amount": amount,
        "metadata": metadata or {},
        "policy_version": policy["policy_version"],
        "chain_branch": chain_branch,
    }
    request_hash = _event_request_hash(request_payload)
    legacy_branchless_request_hash = (
        _legacy_branchless_event_request_hash(request_payload)
        if chain_branch == "main"
        else None
    )
    legacy_burn_address_request_hash = None
    if legacy_destination_fund_key is None and destination_fund_key == "burn":
        legacy_payload = dict(request_payload)
        legacy_payload["destination_fund_key"] = None
        legacy_burn_address_request_hash = _event_request_hash(legacy_payload)
    existing = conn.execute("SELECT * FROM points_economy_events WHERE idempotency_key=?", (str(idempotency_key),)).fetchone()
    if existing:
        if str(existing["chain_branch"] if "chain_branch" in existing.keys() else "main") != chain_branch:
            raise ValueError("economy idempotency key belongs to a different chain branch")
        accepted_hashes = {request_hash}
        if legacy_branchless_request_hash:
            accepted_hashes.add(legacy_branchless_request_hash)
        if legacy_burn_address_request_hash:
            accepted_hashes.add(legacy_burn_address_request_hash)
            if chain_branch == "main":
                accepted_hashes.add(_legacy_branchless_event_request_hash(legacy_payload))
        if existing["request_hash"] not in accepted_hashes:
            raise ValueError("economy idempotency key conflict")
        return existing, False

    replay = None
    if not skip_preflight_replay:
        replay = replay_economy_events(conn, policy=policy, chain_secret=chain_secret, persist_cache=False, chain_branch=chain_branch)
    if source_fund_key == "mint" and not allow_mint_override:
        if replay is None:
            replay = replay_economy_events(conn, policy=policy, chain_secret=chain_secret, persist_cache=False, chain_branch=chain_branch)
        releasable = int(policy["max_supply"]) - int(policy["reserved_locked"])
        if int(replay["minted_total"]) + amount > releasable:
            if int(replay["minted_total"]) >= releasable:
                raise ValueError("mint_supply_exhausted")
            raise ValueError("mint would exceed releasable supply")
        expanded_portion = _expanded_supply_mint_portion(policy, replay, amount)
        if expanded_portion > 0:
            restriction = _policy_supply_expansion_restriction(policy)
            restricted_destination = str(restriction.get("destination_fund_key") or "").strip().lower()
            if not restricted_destination:
                raise ValueError("supply_expansion_authorization_required")
            if destination_fund_key != restricted_destination:
                raise ValueError("mint destination violates supply expansion restriction")
    elif source_fund_key is not None and source_fund_key != "mint":
        if replay is None:
            replay = replay_economy_events(conn, policy=policy, chain_secret=chain_secret, persist_cache=False, chain_branch=chain_branch)
        source_balance = int((replay.get("balances") or {}).get(source_fund_key, {}).get("balance") or 0)
        if source_balance < amount:
            raise ValueError("source fund balance is insufficient")

    now = utc_now()
    event_uuid = str(uuid.uuid4())
    previous_hash = _last_economy_event_hash(conn, chain_branch=chain_branch)
    source_address = wallets[source_fund_key]["address"] if source_fund_key else source_address
    destination_address = wallets[destination_fund_key]["address"] if destination_fund_key else destination_address
    row_payload = {
        "event_uuid": event_uuid,
        "event_type": str(event_type),
        "transaction_type": str(transaction_type),
        "source_fund_key": source_fund_key,
        "source_address": source_address,
        "destination_fund_key": destination_fund_key,
        "destination_address": destination_address,
        "amount": amount,
        "idempotency_key": str(idempotency_key),
        "request_hash": request_hash,
        "policy_version": policy["policy_version"],
        "metadata_json": _json_dumps(metadata or {}),
        "previous_event_hash": previous_hash,
        "created_at": now,
    }
    event_hash = compute_economy_event_hash(row_payload)
    cur = conn.execute(
        """
        INSERT INTO points_economy_events (
            event_uuid, chain_branch, event_type, transaction_type, source_fund_key, source_address,
            destination_fund_key, destination_address, amount, idempotency_key,
            request_hash, policy_version, status, metadata_json, previous_event_hash,
            event_hash, created_by, created_by_role, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?, ?, ?, ?, ?, ?)
        """,
        (
            event_uuid,
            chain_branch,
            row_payload["event_type"],
            row_payload["transaction_type"],
            source_fund_key,
            source_address,
            destination_fund_key,
            destination_address,
            amount,
            str(idempotency_key),
            request_hash,
            policy["policy_version"],
            row_payload["metadata_json"],
            previous_hash,
            event_hash,
            _actor_value(actor, "id"),
            _actor_value(actor, "role"),
            now,
        ),
    )
    return conn.execute("SELECT * FROM points_economy_events WHERE id=?", (cur.lastrowid,)).fetchone(), True


def _actor_value(actor, key):
    if not actor:
        return None
    if hasattr(actor, "keys"):
        return actor[key] if key in actor.keys() else None
    return actor.get(key) if hasattr(actor, "get") else None


def bootstrap_economy_layer(conn, *, chain_secret, actor=None, chain_branch="main"):
    policy = load_economy_policy(conn)
    ensure_economy_fund_wallets(conn, chain_secret=chain_secret)
    chain_branch = str(chain_branch or "main").strip() or "main"
    if chain_branch != "main":
        return {"created_event_uuids": [], "created_count": 0, "policy": policy, "branch_bootstrap": "disabled_for_recovery_branch"}
    allocations = (
        ("official_treasury", "treasury_allocation", int(policy["official_treasury_initial"])),
        ("promo_fund", "promo_allocation", int(policy["promo_fund_initial"])),
        ("exchange_fund", "exchange_allocation", int(policy["exchange_fund_initial"])),
    )
    created = []
    for fund_key, transaction_type, amount in allocations:
        row, was_created = append_economy_event(
            conn,
            chain_secret=chain_secret,
            event_type="mint",
            transaction_type=transaction_type,
            source_fund_key="mint",
            destination_fund_key=fund_key,
            amount=amount,
            idempotency_key=f"economy_bootstrap:{policy['policy_version']}:{fund_key}:{amount}",
            metadata={"bootstrap": True, "phase": "1A"},
            actor=actor,
            chain_branch=chain_branch,
        )
        if was_created:
            created.append(row["event_uuid"])
    return {"created_event_uuids": created, "created_count": len(created), "policy": policy}


def replay_economy_events(conn, *, policy=None, chain_secret, persist_cache=False, chain_branch="main"):
    policy = policy or load_economy_policy(conn)
    wallets = ensure_economy_fund_wallets(conn, chain_secret=chain_secret)
    chain_branch = str(chain_branch or "main").strip() or "main"
    balances = {
        fund_key: {
            "fund_key": fund_key,
            "address": row["address"],
            "label": row["label"],
            "balance": 0,
            "custody_mode": row["custody_mode"],
            "wallet_status": "active",
            "derived_cache": True,
            "updated_at": row["updated_at"],
        }
        for fund_key, row in wallets.items()
    }
    rows = conn.execute(
        "SELECT * FROM points_economy_events WHERE status='confirmed' AND chain_branch=? ORDER BY id ASC",
        (chain_branch,),
    ).fetchall()
    minted_total = 0
    burned_total = 0
    external_balances = {}
    exchange_receivable_principal = 0
    for row in rows:
        amount = int(row["amount"])
        source = row["source_fund_key"]
        dest = row["destination_fund_key"]
        source_address = str(row["source_address"] or "")
        dest_address = str(row["destination_address"] or "")
        transaction_type = str(row["transaction_type"] or "")
        if source == "burn" or _is_burn_address(source_address, wallets=wallets):
            raise ValueError("economy replay found unspendable burn address as source")
        if source == "mint":
            minted_total += amount
        elif source:
            balances[source]["balance"] -= amount
            if balances[source]["balance"] < 0:
                raise ValueError(f"economy replay would create negative balance for {source}")
        elif source_address:
            external_balances[source_address] = int(external_balances.get(source_address, 0)) - amount
        if dest == "burn" or (not dest and _is_burn_address(dest_address, wallets=wallets)):
            burned_total += amount
            balances["burn"]["balance"] += amount
        elif dest:
            balances[dest]["balance"] += amount
        elif dest_address:
            external_balances[dest_address] = int(external_balances.get(dest_address, 0)) + amount
        if (
            source == "exchange_fund"
            and not dest
            and transaction_type in EXCHANGE_PRINCIPAL_LENT_TYPES
        ):
            exchange_receivable_principal += amount
        elif (
            not source
            and dest == "exchange_fund"
            and transaction_type in EXCHANGE_PRINCIPAL_REPAID_TYPES
        ):
            exchange_receivable_principal -= amount
            if exchange_receivable_principal < 0:
                raise ValueError("economy replay would create negative exchange principal receivable")

    negative_external = {
        address: balance
        for address, balance in external_balances.items()
        if int(balance or 0) < 0
    }
    if negative_external:
        first_address = sorted(negative_external)[0]
        raise ValueError(f"economy replay would create negative external balance for {first_address}")
    releasable_supply = int(policy["max_supply"]) - int(policy["reserved_locked"])
    if minted_total > releasable_supply:
        raise ValueError("economy replay exceeds releasable supply")
    active_supply = minted_total - burned_total
    if active_supply < 0:
        raise ValueError("economy replay would create negative active supply")
    mint_remaining = int(policy["max_supply"]) - minted_total
    balances["mint"]["balance"] = mint_remaining
    balances["burn"]["balance"] = burned_total
    last_hash = rows[-1]["event_hash"] if rows else None
    fund_supply = sum(
        int(balances.get(key, {}).get("balance") or 0)
        for key in ("official_treasury", "promo_fund", "exchange_fund")
    )
    circulating_supply = active_supply - fund_supply
    if circulating_supply < 0:
        raise ValueError("economy replay would create negative circulating supply")
    external_supply = sum(int(value or 0) for value in external_balances.values())
    if external_supply != circulating_supply:
        raise ValueError("economy replay external balances do not match circulating supply")
    wallet_root_hash = sha256_text(canonical_json({
        "external": {key: value for key, value in sorted(external_balances.items()) if int(value or 0)},
        "funds": {key: item["balance"] for key, item in sorted(balances.items())},
    }))
    health = economy_health(
        policy=policy,
        minted_total=minted_total,
        balances=balances,
        exchange_receivable_principal=exchange_receivable_principal,
    )
    result = {
        "policy": policy,
        "chain_branch": chain_branch,
        "balances": balances,
        "external_balances": {key: value for key, value in sorted(external_balances.items()) if int(value or 0)},
        "external_supply": external_supply,
        "exchange_receivable_principal": exchange_receivable_principal,
        "exchange_total_assets": int(balances.get("exchange_fund", {}).get("balance") or 0) + exchange_receivable_principal,
        "minted_total": minted_total,
        "burned_total": burned_total,
        "active_supply": active_supply,
        "max_supply": int(policy["max_supply"]),
        "reserved_locked": int(policy["reserved_locked"]),
        "releasable_supply": releasable_supply,
        "mint_remaining": mint_remaining,
        "releasable_remaining": releasable_supply - minted_total,
        "fund_supply": fund_supply,
        "circulating_supply": circulating_supply,
        "event_count": len(rows),
        "replay_height": len(rows),
        "replay_event_hash": last_hash,
        "wallet_root_hash": wallet_root_hash,
        "health": health,
    }
    if persist_cache:
        rebuild_economy_derived_balances(conn, replay=result)
    return result


def economy_health(*, policy, minted_total, balances, exchange_receivable_principal=0):
    max_supply = int(policy["max_supply"])
    releasable_supply = max(0, max_supply - int(policy.get("reserved_locked") or 0))
    mint_remaining = max_supply - int(minted_total)
    releasable_remaining = releasable_supply - int(minted_total)
    mint_remaining_ratio = mint_remaining / max_supply if max_supply else 0
    releasable_remaining_ratio = releasable_remaining / releasable_supply if releasable_supply else 0
    promo_balance = int(balances.get("promo_fund", {}).get("balance") or 0)
    exchange_balance = int(balances.get("exchange_fund", {}).get("balance") or 0)
    exchange_receivable_principal = max(0, int(exchange_receivable_principal or 0))
    exchange_assets = exchange_balance + exchange_receivable_principal
    status = "green"
    reasons = []
    if releasable_remaining <= 0:
        status = "red"
        reasons.append("releasable_supply_exhausted")
    elif releasable_remaining_ratio < 0.2:
        status = "red"
        reasons.append("releasable_remaining_below_20_percent")
    elif releasable_remaining_ratio < 0.5:
        status = "yellow"
        reasons.append("releasable_remaining_below_50_percent")
    if mint_remaining_ratio < 0.2:
        status = "red"
        reasons.append("mint_remaining_below_20_percent")
    elif mint_remaining_ratio < 0.5 and status != "red":
        status = "yellow"
        reasons.append("mint_remaining_below_50_percent")
    if promo_balance < int(policy["promo_critical_watermark"]):
        status = "red"
        reasons.append("promo_fund_critical")
    elif promo_balance < int(policy["promo_low_watermark"]) and status != "red":
        status = "yellow"
        reasons.append("promo_fund_low")
    if exchange_assets < int(policy["exchange_critical_watermark"]):
        status = "red"
        reasons.append("exchange_fund_assets_critical")
    elif exchange_assets < int(policy["exchange_low_watermark"]) and status != "red":
        status = "yellow"
        reasons.append("exchange_fund_assets_low")
    elif exchange_balance < int(policy["exchange_critical_watermark"]) and status != "red":
        status = "yellow"
        reasons.append("exchange_fund_liquidity_critical")
    elif exchange_balance < int(policy["exchange_low_watermark"]) and status == "green":
        status = "yellow"
        reasons.append("exchange_fund_liquidity_low")
    return {"status": status, "reasons": reasons or ["ok"]}


def _int_value(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def economy_supply_equation_report(*, replay, circulation=None):
    """Build the root closed-loop supply equation from replay and wallet cache."""

    circulation = circulation if isinstance(circulation, dict) else {}
    balances = replay.get("balances") if isinstance(replay.get("balances"), dict) else {}
    promo_balance = _int_value((balances.get("promo_fund") or {}).get("balance"))
    official_balance = _int_value((balances.get("official_treasury") or {}).get("balance"))
    exchange_balance = _int_value((balances.get("exchange_fund") or {}).get("balance"))
    exchange_settlement_withheld_contra = max(
        0,
        _int_value(circulation.get("exchange_margin_settlement_withheld_contra_points")),
    )
    exchange_bridge_balance = max(0, exchange_balance - exchange_settlement_withheld_contra)
    legacy_outstanding = _int_value(circulation.get("member_outstanding_points"))
    member_available = _int_value(circulation.get("member_available_points"))
    member_frozen = _int_value(circulation.get("member_frozen_points"))
    root_outstanding = _int_value(circulation.get("root_outstanding_points"))
    total_legacy_outstanding = legacy_outstanding + root_outstanding
    economy_circulating = _int_value(replay.get("circulating_supply"))
    economy_external_supply = _int_value(replay.get("external_supply"))
    if not economy_external_supply and economy_circulating:
        economy_external_supply = economy_circulating
    residual_off_wallet_external = max(0, economy_external_supply - total_legacy_outstanding)
    explicit_off_wallet_external = circulation.get("off_wallet_economy_external_points")
    off_wallet_external = (
        max(0, _int_value(explicit_off_wallet_external))
        if explicit_off_wallet_external is not None
        else residual_off_wallet_external
    )
    economy_pc0_member = _int_value(circulation.get("economy_pc0_member_internal_points"))
    economy_pc0_root = _int_value(circulation.get("economy_pc0_root_internal_points"))
    economy_pc0_unknown = _int_value(circulation.get("economy_pc0_unknown_internal_points"))
    economy_pc0_internal = _int_value(circulation.get("economy_pc0_internal_points"))
    if economy_pc0_internal <= 0:
        economy_pc0_internal = economy_pc0_member + economy_pc0_root + economy_pc0_unknown
    if economy_pc0_internal <= 0 and explicit_off_wallet_external is None:
        economy_pc0_internal = max(0, economy_external_supply - off_wallet_external)
    ledger_vs_economy_gap = circulation.get("ledger_vs_economy_external_gap_points")
    if ledger_vs_economy_gap is None and explicit_off_wallet_external is not None:
        ledger_vs_economy_gap = total_legacy_outstanding - (economy_pc0_member + economy_pc0_root)
    ledger_vs_economy_gap = _int_value(ledger_vs_economy_gap)
    bridge_flow_totals = circulation.get("bridge_flow_totals") if isinstance(circulation.get("bridge_flow_totals"), dict) else {}
    max_supply = _int_value(replay.get("max_supply"))
    burned_total = _int_value(replay.get("burned_total"))
    mint_remaining = _int_value(replay.get("mint_remaining"))

    if explicit_off_wallet_external is not None:
        unfunded_legacy = max(0, ledger_vs_economy_gap)
    else:
        unfunded_legacy = max(0, total_legacy_outstanding - economy_external_supply)
    promo_after_required_debit = promo_balance - unfunded_legacy
    actual_total = burned_total + official_balance + exchange_balance + promo_balance + economy_external_supply + mint_remaining
    bridged_total = (
        burned_total
        + official_balance
        + exchange_bridge_balance
        + promo_after_required_debit
        + total_legacy_outstanding
        + off_wallet_external
        + mint_remaining
    )
    actual_gap = actual_total - max_supply
    bridged_gap = bridged_total - max_supply
    formula_balanced = bridged_gap == 0
    audit_balanced = formula_balanced and ledger_vs_economy_gap == 0
    status = "balanced"
    if unfunded_legacy > promo_balance:
        status = "blocker"
    elif actual_gap != 0 or bridged_gap != 0 or ledger_vs_economy_gap != 0:
        status = "legacy_gap"

    return {
        "phase": "1B_walletized_replay",
        "status": status,
        "legacy_outstanding_points": legacy_outstanding,
        "member_internal_available_points": member_available,
        "member_internal_frozen_points": member_frozen,
        "member_internal_circulating_points": legacy_outstanding,
        "root_outstanding_points": root_outstanding,
        "root_internal_circulating_points": root_outstanding,
        "total_legacy_outstanding_points": total_legacy_outstanding,
        "total_internal_circulating_points": total_legacy_outstanding,
        "wallet_ledger_outstanding_points": total_legacy_outstanding,
        "economy_external_circulating_points": economy_external_supply,
        "off_wallet_economy_external_points": off_wallet_external,
        "residual_off_wallet_economy_external_points": residual_off_wallet_external,
        "economy_pc0_member_internal_points": economy_pc0_member,
        "economy_pc0_root_internal_points": economy_pc0_root,
        "economy_pc0_unknown_internal_points": economy_pc0_unknown,
        "economy_pc0_internal_points": economy_pc0_internal,
        "ledger_vs_economy_external_gap_points": ledger_vs_economy_gap,
        "economy_external_balance_breakdown": circulation.get("economy_external_balance_breakdown") or {},
        "bridge_flow_totals": bridge_flow_totals,
        "exchange_fund_receivable_principal_points": _int_value(replay.get("exchange_receivable_principal")),
        "exchange_fund_total_assets_points": _int_value(replay.get("exchange_total_assets")),
        "burned_total": burned_total,
        "system_burn_sink_balance": burned_total,
        "system_mint_unissued_balance": mint_remaining,
        "pc0_platform_internal_fund_balance": official_balance + exchange_balance + promo_balance,
        "holder_circulating_balance": economy_external_supply,
        "official_treasury_balance": official_balance,
        "exchange_fund_balance": exchange_balance,
        "exchange_fund_reconciled_balance": exchange_bridge_balance,
        "exchange_margin_settlement_withheld_contra_points": exchange_settlement_withheld_contra,
        "promo_fund_balance": promo_balance,
        "mint_remaining": mint_remaining,
        "max_supply": max_supply,
        "economy_circulating_supply": economy_circulating,
        "holder_circulating_breakdown": {
            "member_internal_circulating_points": legacy_outstanding,
            "member_internal_available_points": member_available,
            "member_internal_frozen_points": member_frozen,
            "root_internal_circulating_points": root_outstanding,
            "off_wallet_economy_external_points": off_wallet_external,
            "residual_off_wallet_economy_external_points": residual_off_wallet_external,
            "economy_pc0_internal_points": economy_pc0_internal,
            "ledger_vs_economy_external_gap_points": ledger_vs_economy_gap,
            "bridge_flow_totals": bridge_flow_totals,
        },
        "unfunded_legacy_outstanding_points": unfunded_legacy,
        "promo_debit_required_points": unfunded_legacy,
        "promo_balance": promo_balance,
        "promo_balance_after_required_debit": promo_after_required_debit,
        "actual_supply_equation_total": actual_total,
        "actual_supply_equation_gap_points": actual_gap,
        "bridged_supply_equation_total": bridged_total,
        "bridged_supply_equation_gap_points": bridged_gap,
        "bridged_supply_equation_balanced": formula_balanced,
        "formula_gap_balanced": formula_balanced,
        "audit_reconciliation_balanced": audit_balanced,
        "formula": "system_burn_sink + pc0_platform_funds + member_internal_circulating + root_internal_circulating + off_wallet_circulating + system_mint_unissued = max_supply",
        "note": "pc0 is the platform-custodial internal ledger namespace. Mint and burn are system-special chain accounting addresses, not official pc0 wallets.",
    }


def economy_legacy_bridge_report(*, replay, circulation=None):
    return economy_supply_equation_report(replay=replay, circulation=circulation)


def rebuild_economy_derived_balances(conn, *, replay=None, chain_secret=None, chain_branch="main"):
    if replay is None:
        replay = replay_economy_events(conn, chain_secret=chain_secret, persist_cache=False, chain_branch=chain_branch)
    now = utc_now()
    conn.execute("DELETE FROM points_economy_derived_balances")
    for fund_key, item in sorted((replay.get("balances") or {}).items()):
        conn.execute(
            """
            INSERT INTO points_economy_derived_balances (
                fund_key, address, balance, derived_cache, replay_height,
                replay_event_hash, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (
                fund_key,
                item["address"],
                int(item["balance"]),
                int(replay["replay_height"]),
                replay.get("replay_event_hash"),
                now,
            ),
        )
    return {"rebuilt": True, "replay_height": int(replay["replay_height"]), "wallet_root_hash": replay["wallet_root_hash"]}


def verify_economy_derived_balances(conn, *, replay=None, chain_secret=None, chain_branch="main"):
    if replay is None:
        replay = replay_economy_events(conn, chain_secret=chain_secret, persist_cache=False, chain_branch=chain_branch)
    cached = {
        row["fund_key"]: dict(row)
        for row in conn.execute("SELECT * FROM points_economy_derived_balances").fetchall()
    }
    mismatches = []
    for fund_key, item in sorted((replay.get("balances") or {}).items()):
        row = cached.get(fund_key)
        if not row:
            mismatches.append({"fund_key": fund_key, "reason": "missing_cache_row"})
            continue
        if int(row["derived_cache"] or 0) != 1:
            mismatches.append({"fund_key": fund_key, "reason": "not_marked_derived_cache"})
        if row["address"] != item["address"]:
            mismatches.append({"fund_key": fund_key, "reason": "address_mismatch"})
        if int(row["balance"]) != int(item["balance"]):
            mismatches.append({"fund_key": fund_key, "reason": "balance_mismatch"})
        if int(row["replay_height"]) != int(replay["replay_height"]):
            mismatches.append({"fund_key": fund_key, "reason": "replay_height_mismatch"})
        if (row["replay_event_hash"] or "") != (replay.get("replay_event_hash") or ""):
            mismatches.append({"fund_key": fund_key, "reason": "replay_hash_mismatch"})
    for fund_key in sorted(set(cached) - set((replay.get("balances") or {}))):
        mismatches.append({"fund_key": fund_key, "reason": "orphan_cache_row"})
    return {
        "ok": not mismatches,
        "mismatches": mismatches,
        "replay_height": int(replay["replay_height"]),
        "wallet_root_hash": replay["wallet_root_hash"],
    }


def create_economy_snapshot(conn, *, replay=None, chain_secret=None, metadata=None, chain_branch="main"):
    if replay is None:
        replay = replay_economy_events(conn, chain_secret=chain_secret, persist_cache=False, chain_branch=chain_branch)
    event_hash = replay.get("replay_event_hash")
    existing = conn.execute(
        """
        SELECT * FROM points_economy_snapshots
        WHERE snapshot_height=? AND event_hash IS ? AND wallet_root_hash=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (int(replay["replay_height"]), event_hash, replay["wallet_root_hash"]),
    ).fetchone()
    if existing:
        snapshot = dict(existing)
        snapshot["created"] = False
        return snapshot
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO points_economy_snapshots (
            snapshot_uuid, snapshot_height, event_hash, wallet_root_hash,
            minted_total, burned_total, active_supply, circulating_supply,
            metadata_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            int(replay["replay_height"]),
            event_hash,
            replay["wallet_root_hash"],
            int(replay["minted_total"]),
            int(replay["burned_total"]),
            int(replay["active_supply"]),
            int(replay["circulating_supply"]),
            _json_dumps({
                "policy_version": replay["policy"]["policy_version"],
                "phase": "1A",
                "source": "replay",
                **(metadata or {}),
            }),
            now,
        ),
    )
    snapshot = dict(conn.execute("SELECT * FROM points_economy_snapshots WHERE id=?", (cur.lastrowid,)).fetchone())
    snapshot["created"] = True
    return snapshot


def append_economy_incident(conn, *, severity, category, trigger, automatic_actions=None, metadata=None):
    severity = str(severity or "").strip().lower()
    if severity not in ECONOMY_INCIDENT_SEVERITIES:
        raise ValueError("unsupported economy incident severity")
    now = utc_now()
    cur = conn.execute(
        """
        INSERT INTO points_economy_incidents (
            incident_uuid, severity, category, trigger, status,
            automatic_actions_json, metadata_json, created_at
        ) VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
        """,
        (
            str(uuid.uuid4()),
            severity,
            str(category or ""),
            str(trigger or ""),
            _json_dumps(list(automatic_actions or [])),
            _json_dumps(metadata or {}),
            now,
        ),
    )
    return conn.execute("SELECT * FROM points_economy_incidents WHERE id=?", (cur.lastrowid,)).fetchone()


def economy_layer_report(conn, *, chain_secret, actor=None, circulation=None, chain_branch="main"):
    chain_branch = str(chain_branch or "main").strip() or "main"
    bootstrap = bootstrap_economy_layer(conn, chain_secret=chain_secret, actor=actor, chain_branch=chain_branch)
    replay = replay_economy_events(conn, policy=bootstrap["policy"], chain_secret=chain_secret, persist_cache=False, chain_branch=chain_branch)
    derived = rebuild_economy_derived_balances(conn, replay=replay)
    derived_verify = verify_economy_derived_balances(conn, replay=replay)
    snapshot = create_economy_snapshot(conn, replay=replay)
    incidents = [
        dict(row)
        for row in conn.execute(
            """
            SELECT * FROM points_economy_incidents
            ORDER BY id DESC
            LIMIT 20
            """
        ).fetchall()
    ]
    supply_equation = economy_supply_equation_report(replay=replay, circulation=circulation)
    return {
        "phase": "1A",
        "chain_branch": chain_branch,
        "guardrail": "append_only_replay_source_of_truth",
        "bootstrap": {"created_count": bootstrap["created_count"]},
        "policy": replay["policy"],
        "funds": replay["balances"],
        "supply": {
            "max_supply": replay["max_supply"],
            "reserved_locked": replay["reserved_locked"],
            "releasable_supply": replay["releasable_supply"],
            "minted_total": replay["minted_total"],
            "burned_total": replay["burned_total"],
            "active_supply": replay["active_supply"],
            "fund_supply": replay["fund_supply"],
            "circulating_supply": replay["circulating_supply"],
            "external_supply": replay["external_supply"],
            "mint_remaining": replay["mint_remaining"],
            "releasable_remaining": replay["releasable_remaining"],
            "exchange_receivable_principal": replay["exchange_receivable_principal"],
            "exchange_total_assets": replay["exchange_total_assets"],
        },
        "replay": {
            "height": replay["replay_height"],
            "event_hash": replay["replay_event_hash"],
            "wallet_root_hash": replay["wallet_root_hash"],
            "derived_cache": True,
            "derived_rebuild": derived,
            "derived_verify": derived_verify,
            "snapshot": snapshot,
        },
        "legacy_bridge": supply_equation,
        "supply_equation": supply_equation,
        "health": replay["health"],
        "incidents": incidents,
    }
