import uuid
import json
from datetime import datetime, timedelta, timezone


STORAGE_UPGRADE_PRODUCT_ALIASES = {
    "birthday_storage_1gb_30d": "birthday_storage_1gb_7d",
}

STORAGE_UPGRADE_PRODUCTS = {
    "cloud_storage_1gb_7d": {
        "storage_bytes": 1024 ** 3,
        "duration_days": 7,
        "label": "雲端容量 1GB / 7 天",
    },
    "cloud_storage_1gb_30d": {
        "storage_bytes": 1024 ** 3,
        "duration_days": 30,
        "label": "雲端容量 1GB / 30 天",
    },
    "birthday_storage_1gb_7d": {
        "storage_bytes": 1024 ** 3,
        "duration_days": 7,
        "label": "生日禮 1GB / 7 天",
    },
}

STORAGE_UPGRADE_PRICE_DEFAULTS = {
    "cloud_storage_1gb_7d": {
        "item_name": "雲端容量 1GB / 7 天",
        "category": "cloud_drive",
        "currency_type": "soft",
        "base_price": 100,
        "dynamic_pricing": 0,
        "min_price": 50,
        "max_price": 500,
        "enabled": 1,
        "metadata_json": json.dumps({
            "storage_bytes": 1024 ** 3,
            "duration_days": 7,
            "label": "雲端容量 1GB / 7 天",
        }, ensure_ascii=False, separators=(",", ":")),
    },
    "cloud_storage_1gb_30d": {
        "item_name": "雲端容量 1GB / 30 天",
        "category": "cloud_drive",
        "currency_type": "soft",
        "base_price": 400,
        "dynamic_pricing": 0,
        "min_price": 200,
        "max_price": 2000,
        "enabled": 1,
        "metadata_json": json.dumps({
            "storage_bytes": 1024 ** 3,
            "duration_days": 30,
            "label": "雲端容量 1GB / 30 天",
        }, ensure_ascii=False, separators=(",", ":")),
    },
}

BIRTHDAY_STORAGE_GIFT_ITEM_KEY = "birthday_storage_1gb_7d"
BIRTHDAY_STORAGE_GIFT_BYTES = 1024 ** 3
BIRTHDAY_STORAGE_GIFT_DAYS = 7


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_storage_quota_purchase_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS storage_quota_purchases (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            item_key TEXT NOT NULL,
            quantity INTEGER NOT NULL,
            purchased_bytes INTEGER NOT NULL,
            points_spent INTEGER NOT NULL,
            ledger_uuid TEXT,
            starts_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_storage_quota_purchases_user ON storage_quota_purchases(user_id, status, expires_at)"
    )


def normalize_storage_upgrade_product_key(item_key):
    key = str(item_key or "").strip()
    return STORAGE_UPGRADE_PRODUCT_ALIASES.get(key, key)


def storage_upgrade_product(item_key):
    return STORAGE_UPGRADE_PRODUCTS.get(normalize_storage_upgrade_product_key(item_key))


def _metadata_from_item(item):
    if not item:
        return {}
    if isinstance(item.get("metadata"), dict):
        return item["metadata"]
    try:
        return json.loads(item.get("metadata_json") or "{}")
    except Exception:
        return {}


def storage_upgrade_product_from_catalog_item(item):
    if not item:
        return None
    static = storage_upgrade_product(item.get("item_key")) or {}
    metadata = _metadata_from_item(item)
    try:
        storage_bytes = int(metadata.get("storage_bytes") or static.get("storage_bytes") or 0)
        duration_days = int(metadata.get("duration_days") or static.get("duration_days") or 0)
    except Exception:
        return None
    if storage_bytes < 1 or duration_days < 1:
        return None
    return {
        "storage_bytes": storage_bytes,
        "duration_days": duration_days,
        "label": str(metadata.get("label") or static.get("label") or item.get("item_name") or item.get("item_key") or "雲端容量方案"),
    }


def storage_upgrade_product_from_catalog(conn, item_key):
    requested_key = str(item_key or "").strip()
    canonical_key = normalize_storage_upgrade_product_key(requested_key)
    lookup_keys = [canonical_key] + ([requested_key] if requested_key and requested_key != canonical_key else [])
    row = None
    for lookup_key in lookup_keys:
        try:
            row = conn.execute(
                "SELECT * FROM economy_price_catalog WHERE item_key=? AND category='cloud_drive' AND enabled=1",
                (lookup_key,),
            ).fetchone()
        except Exception:
            row = None
        if row:
            break
    return storage_upgrade_product_from_catalog_item(dict(row)) if row else storage_upgrade_product(item_key)


def enrich_storage_upgrade_catalog(items):
    catalog = []
    for item in items or []:
        product = storage_upgrade_product_from_catalog_item(item)
        if not product:
            continue
        catalog.append({
            **dict(item),
            "storage_bytes": int(product["storage_bytes"]),
            "duration_days": int(product["duration_days"]),
            "label": product["label"],
        })
    return catalog


def default_storage_upgrade_catalog():
    rows = []
    for item_key, item in STORAGE_UPGRADE_PRICE_DEFAULTS.items():
        rows.append({
            "item_key": item_key,
            **item,
            "metadata": {},
        })
    return enrich_storage_upgrade_catalog(rows)


def list_storage_upgrade_price_catalog(conn):
    rows = conn.execute(
        """
        SELECT *
        FROM economy_price_catalog
        WHERE category='cloud_drive' AND enabled=1
        ORDER BY base_price, item_key
        """
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.get("metadata_json") or "{}")
        except Exception:
            item["metadata"] = {}
        items.append(item)
    return enrich_storage_upgrade_catalog(items)


def ensure_storage_upgrade_price_catalog(conn):
    ensure_storage_quota_purchase_schema(conn)
    now = _iso(_now())
    for legacy_key, canonical_key in STORAGE_UPGRADE_PRODUCT_ALIASES.items():
        conn.execute(
            """
            UPDATE economy_price_catalog
            SET item_key=?, updated_at=?
            WHERE item_key=?
              AND category='cloud_drive'
              AND NOT EXISTS (SELECT 1 FROM economy_price_catalog WHERE item_key=? AND category='cloud_drive')
            """,
            (canonical_key, now, legacy_key, canonical_key),
        )
        conn.execute(
            "UPDATE economy_price_catalog SET enabled=0, updated_at=? WHERE item_key=? AND category='cloud_drive'",
            (now, legacy_key),
        )
        conn.execute(
            "UPDATE storage_quota_purchases SET item_key=? WHERE item_key=?",
            (canonical_key, legacy_key),
        )
    for item_key, item in STORAGE_UPGRADE_PRICE_DEFAULTS.items():
        conn.execute(
            """
            INSERT OR IGNORE INTO economy_price_catalog (
                item_key, item_name, category, currency_type, base_price,
                dynamic_pricing, min_price, max_price, enabled, metadata_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_key,
                item["item_name"],
                item["category"],
                item["currency_type"],
                item["base_price"],
                item["dynamic_pricing"],
                item["min_price"],
                item["max_price"],
                item["enabled"],
                item["metadata_json"],
                now,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE economy_price_catalog
            SET item_name=?,
                metadata_json=?,
                updated_at=?
            WHERE item_key=? AND category='cloud_drive'
            """,
            (
                item["item_name"],
                item["metadata_json"],
                now,
                item_key,
            ),
        )


def record_storage_quota_purchase(conn, *, user_id, item_key, quantity, points_spent, ledger_uuid=None):
    ensure_storage_quota_purchase_schema(conn)
    product = storage_upgrade_product_from_catalog(conn, item_key)
    if not product:
        raise ValueError("不支援的雲端容量商品")
    quantity = max(1, int(quantity or 1))
    starts_at = _now()
    expires_at = starts_at + timedelta(days=int(product["duration_days"]))
    purchased_bytes = int(product["storage_bytes"]) * quantity
    purchase_id = uuid.uuid4().hex
    conn.execute(
        """
        INSERT INTO storage_quota_purchases (
            id, user_id, item_key, quantity, purchased_bytes, points_spent,
            ledger_uuid, starts_at, expires_at, status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (
            purchase_id,
            int(user_id),
            normalize_storage_upgrade_product_key(item_key),
            quantity,
            purchased_bytes,
            int(points_spent or 0),
            str(ledger_uuid or "") or None,
            _iso(starts_at),
            _iso(expires_at),
            _iso(starts_at),
        ),
    )
    return get_storage_quota_purchase(conn, purchase_id)


def grant_birthday_storage_quota(conn, *, user_id, birthday_year, ledger_uuid=None):
    ensure_storage_quota_purchase_schema(conn)
    year = int(birthday_year)
    if year < 1970:
        raise ValueError("birthday_year is invalid")
    purchase_id = f"birthday_storage:{int(user_id)}:{year}"
    existing = get_storage_quota_purchase(conn, purchase_id)
    if existing:
        return {"ok": True, "created": False, "purchase": existing}
    starts_at = _now()
    expires_at = starts_at + timedelta(days=BIRTHDAY_STORAGE_GIFT_DAYS)
    conn.execute(
        """
        INSERT INTO storage_quota_purchases (
            id, user_id, item_key, quantity, purchased_bytes, points_spent,
            ledger_uuid, starts_at, expires_at, status, created_at
        ) VALUES (?, ?, ?, 1, ?, 0, ?, ?, ?, 'active', ?)
        """,
        (
            purchase_id,
            int(user_id),
            BIRTHDAY_STORAGE_GIFT_ITEM_KEY,
            BIRTHDAY_STORAGE_GIFT_BYTES,
            str(ledger_uuid or "") or None,
            _iso(starts_at),
            _iso(expires_at),
            _iso(starts_at),
        ),
    )
    return {"ok": True, "created": True, "purchase": get_storage_quota_purchase(conn, purchase_id)}


def get_storage_quota_purchase(conn, purchase_id):
    ensure_storage_quota_purchase_schema(conn)
    row = conn.execute("SELECT * FROM storage_quota_purchases WHERE id=?", (str(purchase_id),)).fetchone()
    return dict(row) if row else None


def active_storage_quota_purchases(conn, user_id, *, now=None, ensure_schema=True):
    if ensure_schema:
        ensure_storage_quota_purchase_schema(conn)
    now_iso = _iso(now or _now())
    try:
        rows = conn.execute(
            """
            SELECT * FROM storage_quota_purchases
            WHERE user_id=? AND status='active' AND expires_at>?
            ORDER BY expires_at ASC, created_at ASC
            """,
            (int(user_id), now_iso),
        ).fetchall()
    except Exception:
        if ensure_schema:
            raise
        return []
    return [dict(row) for row in rows]


def purchased_storage_summary(conn, user_id, *, now=None, ensure_schema=True):
    purchases = active_storage_quota_purchases(conn, user_id, now=now, ensure_schema=ensure_schema)
    total = sum(int(row.get("purchased_bytes") or 0) for row in purchases)
    latest_expiry = max((row.get("expires_at") for row in purchases), default=None)
    return {
        "purchased_extra_bytes": int(total),
        "active_purchases": purchases,
        "active_purchase_count": len(purchases),
        "latest_expires_at": latest_expiry,
    }


def get_user_purchased_storage_bytes(conn, user_id):
    return int(purchased_storage_summary(conn, user_id).get("purchased_extra_bytes") or 0)
