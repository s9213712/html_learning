import json
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


APPEARANCE_COLOR_KEYS = (
    "site_bg",
    "site_surface",
    "site_accent",
    "site_accent2",
    "site_text",
    "site_muted",
)
APPEARANCE_THEME_MODES = {"custom", "light", "dark"}
APPEARANCE_LAYOUT_MODES = {"centered", "wide"}
APPEARANCE_DENSITIES = {"comfortable", "compact"}
APPEARANCE_RADIUS_VALUES = {8, 12, 18, 24}
APPEARANCE_FONT_SCALES = {0.95, 1.0, 1.08, 1.15}
APPEARANCE_CONTENT_WIDTHS = {1180, 1380, 1600}
APPEARANCE_FONT_FAMILIES = {"system", "rounded", "serif", "mono"}
APPEARANCE_BACKGROUND_STYLES = {"flat", "aurora", "grid"}
APPEARANCE_PANEL_STYLES = {"glass", "matte", "solid"}
APPEARANCE_SIDEBAR_WIDTHS = {"compact", "standard", "wide"}
PROFILE_TEMPLATES = {"classic", "creator", "compact", "showcase", "gallery", "neon"}
PROFILE_ACCENTS = {"default", "ocean", "sunrise", "forest", "mono", "violet", "ruby"}
PROFILE_DENSITIES = {"comfortable", "compact"}
PROFILE_BANNERS = {"none", "aurora", "neon_grid", "paper", "night_sky", "terminal"}
PROFILE_AVATAR_FRAMES = {"none", "soft_ring", "neon", "pixel", "botanical", "crown"}
PROFILE_AVATAR_SHAPES = {"circle", "rounded", "squircle", "square"}
PROFILE_AVATAR_SIZE_STEPS = {str(value) for value in range(100, 1005, 5)}
PROFILE_AVATAR_SIZES = {"large", "xl", "hero", *PROFILE_AVATAR_SIZE_STEPS}
PROFILE_NAME_FONTS = {"system", "rounded", "serif", "mono", "display"}
PROFILE_NAME_SIZES = {"normal", "large", "hero"}
PROFILE_STICKERS = {"none", "sparkles", "star", "heart", "music", "game", "code", "crown"}
PROFILE_DECORATIONS = {"none", "minimal", "badges", "ribbon", "constellation"}
PROFILE_BACKGROUND_TONES = {"soft", "standard", "bold"}
PROFILE_STYLE_DEFAULTS = {
    "banner": "none",
    "avatar_frame": "soft_ring",
    "avatar_shape": "circle",
    "avatar_size": "140",
    "name_font": "system",
    "name_size": "large",
    "sticker": "none",
    "decoration": "minimal",
    "background_tone": "standard",
}
PUBLIC_ACCOUNT_PROFILE_FIELDS = {"nickname", "real_name", "birthdate", "phone"}
DISPLAY_TIMEZONE_AUTO = "auto"
FIXED_DISPLAY_TIMEZONES = tuple(
    "Etc/GMT+12" if offset == -12 else "Etc/GMT-12" if offset == 12 else f"Etc/GMT{(-offset):+d}"
    for offset in range(-12, 13)
    if offset != 0
)
COMMON_DISPLAY_TIMEZONES = (
    DISPLAY_TIMEZONE_AUTO,
    "UTC",
    *FIXED_DISPLAY_TIMEZONES,
    "Asia/Taipei",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Asia/Hong_Kong",
    "Asia/Shanghai",
    "America/Los_Angeles",
    "America/New_York",
    "Europe/London",
)


def ensure_user_profile_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            display_name TEXT,
            bio TEXT,
            signature TEXT,
            location TEXT,
            website TEXT,
            friend_code TEXT,
            friend_code_rotated_at TEXT,
            custom_title TEXT,
            custom_title_status TEXT NOT NULL DEFAULT 'none',
            profile_visibility TEXT NOT NULL DEFAULT 'public',
            display_timezone TEXT NOT NULL DEFAULT 'auto',
            profile_template TEXT NOT NULL DEFAULT 'classic',
            profile_accent TEXT NOT NULL DEFAULT 'default',
            profile_density TEXT NOT NULL DEFAULT 'comfortable',
            profile_style_json TEXT NOT NULL DEFAULT '{}',
            public_account_fields_json TEXT NOT NULL DEFAULT '[]',
            profile_public_info_json TEXT NOT NULL DEFAULT '[]',
            appearance_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(user_profiles)").fetchall()}
    if "appearance_json" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN appearance_json TEXT NOT NULL DEFAULT '{}'")
    if "friend_code" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN friend_code TEXT")
    if "friend_code_rotated_at" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN friend_code_rotated_at TEXT")
    if "display_timezone" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN display_timezone TEXT NOT NULL DEFAULT 'auto'")
    if "profile_template" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN profile_template TEXT NOT NULL DEFAULT 'classic'")
    if "profile_accent" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN profile_accent TEXT NOT NULL DEFAULT 'default'")
    if "profile_density" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN profile_density TEXT NOT NULL DEFAULT 'comfortable'")
    if "profile_style_json" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN profile_style_json TEXT NOT NULL DEFAULT '{}'")
    if "public_account_fields_json" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN public_account_fields_json TEXT NOT NULL DEFAULT '[]'")
    if "profile_public_info_json" not in columns:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN profile_public_info_json TEXT NOT NULL DEFAULT '[]'")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_profiles_friend_code ON user_profiles(friend_code) WHERE friend_code IS NOT NULL AND friend_code<>''")


def _now():
    return datetime.now().isoformat()


def _clean(value, max_len):
    return str(value or "").strip()[:max_len]


def _visibility(value):
    value = str(value or "public").strip().lower()
    return value if value in {"public", "members", "friends", "private"} else "public"


def _profile_template(value):
    value = str(value or "classic").strip().lower()
    return value if value in PROFILE_TEMPLATES else "classic"


def _profile_accent(value):
    value = str(value or "default").strip().lower()
    return value if value in PROFILE_ACCENTS else "default"


def _profile_density(value):
    value = str(value or "comfortable").strip().lower()
    return value if value in PROFILE_DENSITIES else "comfortable"


def _choice(value, allowed, fallback):
    value = str(value or fallback).strip().lower()
    return value if value in allowed else fallback


def sanitize_profile_style(data):
    data = data if isinstance(data, dict) else {}
    nested = data.get("profile_style")
    if isinstance(nested, dict):
        data = {**data, **nested}
    return {
        "banner": _choice(data.get("banner"), PROFILE_BANNERS, PROFILE_STYLE_DEFAULTS["banner"]),
        "avatar_frame": _choice(data.get("avatar_frame"), PROFILE_AVATAR_FRAMES, PROFILE_STYLE_DEFAULTS["avatar_frame"]),
        "avatar_shape": _choice(data.get("avatar_shape"), PROFILE_AVATAR_SHAPES, PROFILE_STYLE_DEFAULTS["avatar_shape"]),
        "avatar_size": _choice(data.get("avatar_size"), PROFILE_AVATAR_SIZES, PROFILE_STYLE_DEFAULTS["avatar_size"]),
        "name_font": _choice(data.get("name_font"), PROFILE_NAME_FONTS, PROFILE_STYLE_DEFAULTS["name_font"]),
        "name_size": _choice(data.get("name_size"), PROFILE_NAME_SIZES, PROFILE_STYLE_DEFAULTS["name_size"]),
        "sticker": _choice(data.get("sticker"), PROFILE_STICKERS, PROFILE_STYLE_DEFAULTS["sticker"]),
        "decoration": _choice(data.get("decoration"), PROFILE_DECORATIONS, PROFILE_STYLE_DEFAULTS["decoration"]),
        "background_tone": _choice(data.get("background_tone"), PROFILE_BACKGROUND_TONES, PROFILE_STYLE_DEFAULTS["background_tone"]),
    }


def _profile_style_from_row(profile):
    raw = (profile or {}).get("profile_style_json") or "{}"
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        parsed = {}
    return sanitize_profile_style(parsed)


def profile_style_for_payload(profile):
    return _profile_style_from_row(profile)


def sanitize_public_account_fields(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = []
    if not isinstance(value, (list, tuple, set)):
        return []
    out = []
    for item in value:
        key = str(item or "").strip()
        if key in PUBLIC_ACCOUNT_PROFILE_FIELDS and key not in out:
            out.append(key)
    return out


def public_account_fields_for_payload(profile):
    return sanitize_public_account_fields((profile or {}).get("public_account_fields_json") or [])


def sanitize_profile_public_info(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            value = []
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = _clean(item.get("label"), 80)
        text = _clean(item.get("value"), 500)
        if not label or not text:
            continue
        out.append({"label": label, "value": text})
        if len(out) >= 20:
            break
    return out


def public_profile_info_for_payload(profile):
    return sanitize_profile_public_info((profile or {}).get("profile_public_info_json") or [])


def _profile_style_from_update(profile, data):
    existing = _profile_style_from_row(profile)
    nested = data.get("profile_style") if isinstance(data, dict) else None
    if isinstance(nested, dict):
        return sanitize_profile_style({**existing, **nested})
    aliases = {
        "profile_banner": "banner",
        "profile_avatar_frame": "avatar_frame",
        "profile_avatar_size": "avatar_size",
        "profile_name_font": "name_font",
        "profile_name_size": "name_size",
        "profile_sticker": "sticker",
        "profile_decoration": "decoration",
        "profile_background_tone": "background_tone",
    }
    merged = dict(existing)
    for source_key, style_key in aliases.items():
        if source_key in data:
            merged[style_key] = data.get(source_key)
    return sanitize_profile_style(merged)


def normalize_display_timezone(value):
    value = str(value or DISPLAY_TIMEZONE_AUTO).strip()
    if not value or value.lower() in {"auto", "browser", "local"}:
        return DISPLAY_TIMEZONE_AUTO
    if len(value) > 80 or any(ch.isspace() for ch in value):
        return None
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return None
    return value


def _clean_color(value):
    value = str(value or "").strip()
    return value if len(value) == 7 and value.startswith("#") and all(ch in "0123456789abcdefABCDEF" for ch in value[1:]) else ""


def _table_columns(conn, table):
    columns = set()
    for row in conn.execute(f"PRAGMA table_info({table})").fetchall():
        try:
            columns.add(row["name"])
        except Exception:
            columns.add(row[1])
    return columns


def _generate_friend_code(conn):
    for _ in range(20):
        cleaned = "".join(ch for ch in secrets.token_urlsafe(12) if ch.isalnum())
        if len(cleaned) < 12:
            continue
        code = "F" + cleaned[:12]
        if not conn.execute("SELECT 1 FROM user_profiles WHERE friend_code=?", (code,)).fetchone():
            return code
    raise RuntimeError("friend code generation exhausted")


def ensure_profile_friend_code(conn, user_id):
    ensure_user_profile_schema(conn)
    row = conn.execute("SELECT friend_code FROM user_profiles WHERE user_id=?", (int(user_id),)).fetchone()
    if row and row["friend_code"]:
        return row["friend_code"]
    code = _generate_friend_code(conn)
    now = _now()
    conn.execute(
        "UPDATE user_profiles SET friend_code=?, friend_code_rotated_at=?, updated_at=? WHERE user_id=?",
        (code, now, now, int(user_id)),
    )
    return code


def rotate_friend_code(conn, user_id):
    ensure_user_profile_schema(conn)
    get_or_create_profile(conn, user_id)
    code = _generate_friend_code(conn)
    now = _now()
    conn.execute(
        "UPDATE user_profiles SET friend_code=?, friend_code_rotated_at=?, updated_at=? WHERE user_id=?",
        (code, now, now, int(user_id)),
    )
    return code


def sanitize_appearance_settings(data):
    data = data if isinstance(data, dict) else {}
    out = {}
    theme_mode = str(data.get("site_theme_mode") or "").strip().lower()
    if theme_mode in APPEARANCE_THEME_MODES:
        out["site_theme_mode"] = theme_mode
    for key in APPEARANCE_COLOR_KEYS:
        color = _clean_color(data.get(key))
        if color:
            out[key] = color
    layout = str(data.get("site_layout_mode") or "").strip().lower()
    if layout in APPEARANCE_LAYOUT_MODES:
        out["site_layout_mode"] = layout
    density = str(data.get("site_density") or "").strip().lower()
    if density in APPEARANCE_DENSITIES:
        out["site_density"] = density
    try:
        radius = int(data.get("site_radius_px"))
    except Exception:
        radius = None
    if radius in APPEARANCE_RADIUS_VALUES:
        out["site_radius_px"] = radius
    try:
        font_scale = round(float(data.get("site_font_scale")), 2)
    except Exception:
        font_scale = None
    if font_scale in APPEARANCE_FONT_SCALES:
        out["site_font_scale"] = font_scale
    try:
        content_width = int(data.get("site_content_width"))
    except Exception:
        content_width = None
    if content_width in APPEARANCE_CONTENT_WIDTHS:
        out["site_content_width"] = content_width
    font_family = str(data.get("site_font_family") or "").strip().lower()
    if font_family in APPEARANCE_FONT_FAMILIES:
        out["site_font_family"] = font_family
    background_style = str(data.get("site_background_style") or "").strip().lower()
    if background_style in APPEARANCE_BACKGROUND_STYLES:
        out["site_background_style"] = background_style
    panel_style = str(data.get("site_panel_style") or "").strip().lower()
    if panel_style in APPEARANCE_PANEL_STYLES:
        out["site_panel_style"] = panel_style
    sidebar_width = str(data.get("site_sidebar_width") or "").strip().lower()
    if sidebar_width in APPEARANCE_SIDEBAR_WIDTHS:
        out["site_sidebar_width"] = sidebar_width
    return out


def get_profile_appearance(conn, user_id):
    ensure_user_profile_schema(conn)
    row = conn.execute(
        "SELECT appearance_json FROM user_profiles WHERE user_id=?",
        (int(user_id),),
    ).fetchone()
    if not row:
        return {}
    profile = dict(row)
    raw = profile.get("appearance_json") or "{}"
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        parsed = {}
    return sanitize_appearance_settings(parsed)


def get_profile_display_timezone(conn, user_id):
    profile = get_or_create_profile(conn, user_id)
    return normalize_display_timezone(profile.get("display_timezone")) or DISPLAY_TIMEZONE_AUTO


def update_profile_appearance(conn, *, actor, data):
    ensure_user_profile_schema(conn)
    get_or_create_profile(conn, actor["id"])
    appearance = sanitize_appearance_settings(data)
    updated_at = _now()
    conn.execute(
        "UPDATE user_profiles SET appearance_json=?, updated_at=? WHERE user_id=?",
        (json.dumps(appearance, ensure_ascii=False, sort_keys=True), updated_at, int(actor["id"])),
    )
    return appearance


def clear_profile_appearance(conn, *, actor):
    ensure_user_profile_schema(conn)
    get_or_create_profile(conn, actor["id"])
    updated_at = _now()
    conn.execute(
        "UPDATE user_profiles SET appearance_json='{}', updated_at=? WHERE user_id=?",
        (updated_at, int(actor["id"])),
    )
    return {}


def get_or_create_profile(conn, user_id):
    ensure_user_profile_schema(conn)
    row = conn.execute("SELECT * FROM user_profiles WHERE user_id=?", (int(user_id),)).fetchone()
    if row:
        profile = dict(row)
        if not profile.get("friend_code"):
            profile["friend_code"] = ensure_profile_friend_code(conn, user_id)
        return profile
    now = _now()
    code = _generate_friend_code(conn)
    conn.execute(
        """
        INSERT INTO user_profiles (
            user_id, display_name, bio, signature, location, website,
            friend_code, friend_code_rotated_at, profile_visibility,
            display_timezone, profile_template, profile_accent, profile_density,
            profile_style_json, public_account_fields_json, profile_public_info_json,
            appearance_json, created_at, updated_at
        )
        VALUES (?, '', '', '', '', '', ?, ?, 'public', 'auto', 'classic', 'default', 'comfortable', ?, '[]', '[]', '{}', ?, ?)
        """,
        (
            int(user_id),
            code,
            now,
            json.dumps(PROFILE_STYLE_DEFAULTS, ensure_ascii=False, sort_keys=True),
            now,
            now,
        ),
    )
    return dict(conn.execute("SELECT * FROM user_profiles WHERE user_id=?", (int(user_id),)).fetchone())


def update_profile(conn, *, actor, data):
    ensure_user_profile_schema(conn)
    profile = get_or_create_profile(conn, actor["id"])
    updates = {
        "display_name": _clean(data.get("display_name", profile.get("display_name")), 80),
        "bio": _clean(data.get("bio", profile.get("bio")), 1000),
        "signature": _clean(data.get("signature", profile.get("signature")), 300),
        "location": _clean(data.get("location", profile.get("location")), 80),
        "website": _clean(data.get("website", profile.get("website")), 200),
        "profile_visibility": _visibility(data.get("profile_visibility", profile.get("profile_visibility"))),
        "display_timezone": normalize_display_timezone(data.get("display_timezone", profile.get("display_timezone") or DISPLAY_TIMEZONE_AUTO)),
        "profile_template": _profile_template(data.get("profile_template", profile.get("profile_template"))),
        "profile_accent": _profile_accent(data.get("profile_accent", profile.get("profile_accent"))),
        "profile_density": _profile_density(data.get("profile_density", profile.get("profile_density"))),
        "profile_style": _profile_style_from_update(profile, data),
        "public_account_fields": sanitize_public_account_fields(
            data.get("public_account_fields", profile.get("public_account_fields_json") or [])
        ),
        "profile_public_info": sanitize_profile_public_info(
            data.get("profile_public_info", profile.get("profile_public_info_json") or [])
        ),
    }
    if updates["website"] and not (updates["website"].startswith("https://") or updates["website"].startswith("http://")):
        return None, "website 必須以 http:// 或 https:// 開頭"
    if updates["display_timezone"] is None:
        return None, "顯示時區必須是 auto 或有效的 IANA 時區名稱"
    updates["updated_at"] = _now()
    conn.execute(
        """
        UPDATE user_profiles
        SET display_name=?, bio=?, signature=?, location=?, website=?,
            profile_visibility=?, display_timezone=?, profile_template=?,
            profile_accent=?, profile_density=?, profile_style_json=?,
            public_account_fields_json=?, profile_public_info_json=?, updated_at=?
        WHERE user_id=?
        """,
        (
            updates["display_name"],
            updates["bio"],
            updates["signature"],
            updates["location"],
            updates["website"],
            updates["profile_visibility"],
            updates["display_timezone"],
            updates["profile_template"],
            updates["profile_accent"],
            updates["profile_density"],
            json.dumps(updates["profile_style"], ensure_ascii=False, sort_keys=True),
            json.dumps(updates["public_account_fields"], ensure_ascii=False),
            json.dumps(updates["profile_public_info"], ensure_ascii=False),
            updates["updated_at"],
            int(actor["id"]),
        ),
    )
    return get_public_profile(conn, username=actor["username"], viewer=actor), None


def get_public_profile(conn, *, username, viewer=None):
    ensure_user_profile_schema(conn)
    user_cols = _table_columns(conn, "users")
    member_level_expr = "u.member_level" if "member_level" in user_cols else "'' AS member_level"
    effective_level_expr = "u.effective_level" if "effective_level" in user_cols else "'' AS effective_level"
    created_at_expr = "u.created_at" if "created_at" in user_cols else "'' AS created_at"
    deleted_filter = "AND (u.deleted_at IS NULL OR u.deleted_at='')" if "deleted_at" in user_cols else ""
    row = conn.execute(
        f"""
        SELECT u.id, u.username, u.role, {member_level_expr}, {effective_level_expr}, {created_at_expr},
               p.display_name, p.bio, p.signature, p.location, p.website,
               p.friend_code, p.friend_code_rotated_at,
               p.custom_title, p.custom_title_status, p.profile_visibility,
               p.profile_template, p.profile_accent, p.profile_density,
               p.profile_style_json, p.profile_public_info_json,
               p.updated_at AS profile_updated_at
        FROM users u
        LEFT JOIN user_profiles p ON p.user_id=u.id
        WHERE u.username=? {deleted_filter}
        """,
        (str(username or "").strip(),),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["profile_template"] = _profile_template(data.get("profile_template"))
    data["profile_accent"] = _profile_accent(data.get("profile_accent"))
    data["profile_density"] = _profile_density(data.get("profile_density"))
    data["profile_style"] = _profile_style_from_row(data)
    data["profile_public_info"] = public_profile_info_for_payload(data)
    visibility = data.get("profile_visibility") or "public"
    is_owner = viewer and int(viewer.get("id") or -1) == int(data["id"])
    is_member = bool(viewer)
    if visibility == "private" and not is_owner:
        data["bio"] = ""
        data["signature"] = ""
        data["location"] = ""
        data["website"] = ""
        data["profile_public_info"] = []
    if visibility == "members" and not is_member:
        data["bio"] = ""
        data["signature"] = ""
        data["location"] = ""
        data["website"] = ""
        data["profile_public_info"] = []
    if visibility == "friends" and not is_owner:
        data["bio"] = ""
        data["signature"] = ""
        data["location"] = ""
        data["website"] = ""
        data["profile_public_info"] = []
    if not is_owner:
        data.pop("friend_code", None)
        data.pop("friend_code_rotated_at", None)
    return data
