#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import sqlite3
import string
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.system.audit import audit, configure_audit_service
from services.users.auth import hash_password, verify_password
from services.security.password_strength import enforce_password_strength, score_password_strength


def _env_path(name: str, default_path: Path) -> Path:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default_path
    path = Path(raw)
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def default_runtime_dir() -> Path:
    raw = str(os.environ.get("HACKME_RUNTIME_DIR") or "").strip()
    if not raw:
        raise ValueError("HACKME_RUNTIME_DIR or --runtime-dir is required")
    return _env_path("HACKME_RUNTIME_DIR", Path(raw)).resolve()


def default_db_path(runtime_dir: Path) -> Path:
    db_dir = _env_path("HTML_LEARNING_DB_DIR", runtime_dir / "database")
    return db_dir / "database.db"


def _runtime_secret_path(runtime_dir: Path, env_name: str, relative_path: str) -> Path:
    secrets_root = _env_path("HTML_LEARNING_RUNTIME_SECRETS_DIR", runtime_dir)
    return _env_path(env_name, secrets_root / relative_path)


def _load_text_secret(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _load_binary_secret(path: Path) -> bytes:
    return path.read_bytes()


def _build_get_db(db_path: Path):
    def _get_db():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return _get_db


def _configure_runtime_audit(db_path: Path, runtime_dir: Path) -> bool:
    try:
        chain_seed = _load_text_secret(_runtime_secret_path(runtime_dir, "HTML_LEARNING_CHAIN_SEED_PATH", ".chain_seed"))
        integrity_key = _load_binary_secret(_runtime_secret_path(runtime_dir, "HTML_LEARNING_INTEGRITY_KEY_PATH", ".integrity_key"))
        log_dir = _env_path("HTML_LEARNING_LOG_DIR", runtime_dir / "logs")
        anchor_dir = _env_path("HTML_LEARNING_ANCHOR_DIR", runtime_dir / "anchors")
        log_dir.mkdir(parents=True, exist_ok=True)
        anchor_dir.mkdir(parents=True, exist_ok=True)
        configure_audit_service(
            get_db=_build_get_db(db_path),
            chain_seed=chain_seed,
            integrity_key=integrity_key,
            audit_log_path=str(log_dir / "audit.log"),
            audit_anchor_path=str(anchor_dir / "audit_head.jsonl"),
            audit_anchor_latest_path=str(anchor_dir / "audit_head_latest.json"),
            audit_anchor_interval_seconds=60,
        )
        return True
    except Exception:
        return False


def _password_strength_error(password: str) -> str | None:
    if len(password) > 128:
        return "密碼太長（最多 128 字元）"
    ok, msg, _strength = enforce_password_strength(password, min_score=3)
    if not ok:
        return msg
    if len(password) < 12:
        return "離線 root recovery 臨時密碼至少需要 12 個字元"
    return None


def _generate_recovery_password(length: int = 24) -> str:
    upper = string.ascii_uppercase
    lower = string.ascii_lowercase
    digits = string.digits
    symbols = "!@#$%^&*()-_=+"
    alphabet = upper + lower + digits + symbols
    chars = [
        secrets.choice(upper),
        secrets.choice(lower),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]
    while len(chars) < length:
        chars.append(secrets.choice(alphabet))
    for idx in range(len(chars) - 1, 0, -1):
        swap = secrets.randbelow(idx + 1)
        chars[idx], chars[swap] = chars[swap], chars[idx]
    return "".join(chars)


def _prompt_password() -> str:
    first = getpass.getpass("輸入新的 root 臨時密碼：")
    second = getpass.getpass("再次輸入新的 root 臨時密碼：")
    if first != second:
        raise ValueError("兩次密碼輸入不一致")
    return first


def _trim_password_history(conn, user_id: int, limit: int = 5) -> None:
    conn.execute(
        "DELETE FROM user_passwords WHERE user_id=? AND id NOT IN ("
        "SELECT id FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT ?"
        ")",
        (int(user_id), int(user_id), int(limit)),
    )


def _delete_csrf_tokens_for_username(conn, username: str) -> None:
    try:
        conn.execute("DELETE FROM csrf_tokens WHERE username=?", (username,))
    except sqlite3.OperationalError:
        return


def recover_root_password(
    *,
    db_path: Path,
    runtime_dir: Path,
    new_password: str | None = None,
    operator: str = "offline-root-recovery-cli",
    reason: str = "offline root recovery",
) -> dict:
    if not db_path.exists():
        raise FileNotFoundError(f"找不到資料庫：{db_path}")
    password = new_password or _generate_recovery_password()
    err = _password_strength_error(password)
    if err:
        raise ValueError(err)

    audit_ready = _configure_runtime_audit(db_path, runtime_dir)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        user = conn.execute(
            "SELECT id, username, role, status FROM users WHERE username='root' LIMIT 1"
        ).fetchone()
        if not user:
            raise RuntimeError("找不到 root 帳號")
        if str(user["status"] or "").strip().lower() != "active":
            raise RuntimeError("root 帳號目前不是 active，請先確認站台帳號狀態")
        current_pw = conn.execute(
            "SELECT password_hash FROM user_passwords WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (int(user["id"]),),
        ).fetchone()
        if current_pw and verify_password(current_pw["password_hash"], password):
            raise ValueError("新密碼不可與目前密碼相同")
        now = datetime.now().isoformat()
        strength = score_password_strength(password)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
            (int(user["id"]), hash_password(password), now),
        )
        conn.execute(
            """
            UPDATE users
            SET password_strength_score=?, password_changed_at=?, must_change_password=1,
                is_default_password=0, failed_login_count=0, locked_until=NULL, updated_at=?
            WHERE id=?
            """,
            (int(strength["score"]), now, now, int(user["id"])),
        )
        revoked_count = conn.execute(
            "UPDATE sessions SET is_revoked=1, revoked_at=? WHERE user_id=? AND is_revoked=0",
            (now, int(user["id"])),
        ).rowcount
        _delete_csrf_tokens_for_username(conn, "root")
        _trim_password_history(conn, int(user["id"]))
        conn.commit()
    finally:
        conn.close()

    audit_detail = f"operator={operator[:80]}, reason={reason[:160]}, sessions_revoked={max(0, int(revoked_count or 0))}"
    if audit_ready:
        try:
            audit(
                "ROOT_OFFLINE_PASSWORD_RECOVERY",
                "-",
                user="root",
                success=True,
                ua="offline-root-recovery-cli",
                detail=audit_detail,
            )
        except Exception:
            audit_ready = False

    return {
        "ok": True,
        "username": "root",
        "db_path": str(db_path),
        "runtime_dir": str(runtime_dir),
        "temporary_password": password,
        "must_change_password": True,
        "sessions_revoked": max(0, int(revoked_count or 0)),
        "audit_recorded": bool(audit_ready),
        "reason": reason,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="離線 root recovery CLI：直接在 runtime DB 重設 root 密碼並撤銷現有 session。"
    )
    parser.add_argument("--runtime-dir", help="runtime 根目錄；未指定時必須設定 HACKME_RUNTIME_DIR")
    parser.add_argument("--db-path", help="直接指定 database.db；未指定時從 runtime 推導")
    parser.add_argument("--password", help="直接指定新的 root 臨時密碼（不建議，會留在 shell history）")
    parser.add_argument("--prompt-password", action="store_true", help="互動式輸入新的 root 臨時密碼")
    parser.add_argument("--operator", default="offline-root-recovery-cli", help="審計紀錄中的操作者標記")
    parser.add_argument("--reason", default="offline root recovery", help="審計紀錄中的原因說明")
    parser.add_argument("--json", action="store_true", help="以 JSON 輸出結果")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.password and args.prompt_password:
        parser.error("--password 與 --prompt-password 不可同時使用")
    try:
        if args.runtime_dir:
            runtime_dir = Path(args.runtime_dir).expanduser().resolve()
        elif args.db_path:
            explicit_db = Path(args.db_path).expanduser().resolve()
            runtime_dir = explicit_db.parent.parent if explicit_db.parent.name == "database" else explicit_db.parent
        elif str(os.environ.get("HACKME_RUNTIME_DIR") or "").strip():
            runtime_dir = default_runtime_dir()
        else:
            parser.error("HACKME_RUNTIME_DIR, --runtime-dir, or --db-path is required")
        db_path = Path(args.db_path).expanduser().resolve() if args.db_path else default_db_path(runtime_dir).resolve()
        password = args.password
        if args.prompt_password:
            password = _prompt_password()
        result = recover_root_password(
            db_path=db_path,
            runtime_dir=runtime_dir,
            new_password=password,
            operator=str(args.operator or "").strip() or "offline-root-recovery-cli",
            reason=str(args.reason or "").strip() or "offline root recovery",
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "msg": str(exc)}, ensure_ascii=False))
        else:
            print(f"[root-recovery] 失敗：{exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("[root-recovery] 已重設 root 臨時密碼。")
        print(f"database: {result['db_path']}")
        print(f"runtime: {result['runtime_dir']}")
        print(f"sessions_revoked: {result['sessions_revoked']}")
        print(f"audit_recorded: {'yes' if result['audit_recorded'] else 'no'}")
        print("must_change_password: yes")
        print(f"temporary_password: {result['temporary_password']}")
        print("注意：請用這組臨時密碼登入後立即修改；若使用者遺失此臨時密碼，需再次執行本工具。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
