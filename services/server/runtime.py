"""Runtime and environment bootstrap helpers for ``server.py``."""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from ipaddress import ip_address
from pathlib import Path

from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def default_runtime_root_path(app_name="hackme_web"):
    name = Path(str(app_name or "hackme_web")).name or "hackme_web"
    configured_state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    if configured_state_home:
        state_home = Path(configured_state_home).expanduser()
        if not state_home.is_absolute():
            state_home = Path.home() / ".local" / "state"
    else:
        state_home = Path.home() / ".local" / "state"
    return (state_home / name).resolve()


def default_runtime_root(app_name="hackme_web"):
    return str(default_runtime_root_path(app_name))


def _env_path(name, default_path):
    value = os.environ.get(name, "").strip()
    if not value:
        return default_path
    return value if os.path.isabs(value) else os.path.abspath(value)


def _load_db_setting_value(db_path, key):
    if not os.path.exists(db_path):
        return ""
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
            return str(row[0] or "").strip() if row else ""
        finally:
            conn.close()
    except Exception:
        return ""


def _load_or_create_text_secret(env_name, path, *, generator):
    env_value = os.environ.get(env_name, "").strip()
    if env_value:
        return env_value
    if os.path.exists(path):
        try:
            os.chmod(path, 0o600)
            with open(path, encoding="utf-8") as f:
                value = f.read().strip()
            if value:
                return value
        except Exception:
            pass
    value = generator()
    with open(path, "w", encoding="utf-8") as f:
        f.write(value)
    os.chmod(path, 0o600)
    return value


def _load_or_create_binary_secret(env_name, path, *, generator):
    env_value = os.environ.get(env_name)
    if env_value:
        return env_value.encode("utf-8")
    if os.path.exists(path):
        try:
            os.chmod(path, 0o600)
            with open(path, "rb") as f:
                value = f.read()
            if value:
                return value
        except Exception:
            pass
    value = generator()
    with open(path, "wb") as f:
        f.write(value)
    os.chmod(path, 0o600)
    return value


def ensure_local_tls_files(cert_file, key_file):
    if os.path.exists(cert_file) and os.path.exists(key_file):
        return {"created": False, "cert_file": cert_file, "key_file": key_file}

    os.makedirs(os.path.dirname(os.path.abspath(cert_file)), exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "TW"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "hackme_web local"),
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow() - timedelta(minutes=1))
        .not_valid_after(datetime.utcnow() + timedelta(days=825))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ip_address("127.0.0.1")),
                    x509.IPAddress(ip_address("::1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    tmp_key = key_file + ".tmp"
    tmp_cert = cert_file + ".tmp"
    with open(tmp_key, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
    os.chmod(tmp_key, 0o600)
    with open(tmp_cert, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(tmp_cert, 0o644)
    os.replace(tmp_key, key_file)
    os.replace(tmp_cert, cert_file)
    return {"created": True, "cert_file": cert_file, "key_file": key_file}


def load_chain_seed(seed_file):
    if os.path.exists(seed_file):
        try:
            with open(seed_file, encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    seed = secrets.token_hex(24)
    with open(seed_file, "w", encoding="utf-8") as f:
        f.write(seed)
    os.chmod(seed_file, 0o600)
    return seed


def _build_fernet(secret):
    if isinstance(secret, bytes):
        secret = secret.decode("utf-8", errors="ignore")
    secret = str(secret).strip()
    try:
        return Fernet(secret.encode("utf-8"))
    except Exception:
        derived = base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())
        return Fernet(derived)


def load_json(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def parse_ip_set(raw_value):
    if not raw_value:
        return set()
    values = set()
    for token in str(raw_value).split(","):
        token = token.strip()
        if not token:
            continue
        try:
            values.add(str(ip_address(token)))
        except Exception:
            continue
    return values


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def _env_int(name, default, minimum=None):
    raw = os.environ.get(name)
    try:
        value = int(str(raw).strip()) if raw is not None else int(default)
    except Exception:
        value = int(default)
    if minimum is not None and value < minimum:
        value = minimum
    return value


def _env_session_samesite():
    same_site = os.environ.get("SESSION_COOKIE_SAMESITE", "Strict").strip().lower()
    return "Strict" if same_site in {"", "strict"} else ("Lax" if same_site == "lax" else "None")
