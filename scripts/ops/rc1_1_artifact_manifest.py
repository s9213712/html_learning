#!/usr/bin/env python3
"""Create an RC1.1-A artifact manifest and run a narrow secret scan."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.test_artifacts import test_artifact_path  # noqa: E402


DEFAULT_OUT = test_artifact_path("qa", "rc1_1_a_artifact_manifest.json")
DEFAULT_ARTIFACTS = [
    test_artifact_path("qa", "pointschain_rc1_1_gate.json"),
    test_artifact_path("ops", "restore_drill_rc1_1_gate.json"),
    test_artifact_path("anchors", "pointschain_rc1_1_54343_anchor.json"),
    test_artifact_path("anchors", "pointschain_rc1_1_54344_anchor.json"),
]

SECRET_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\.chain_seed",
        r"maintenance[_-]?bypass[_-]?token",
        r"session[_-]?cookie",
        r"admin[_-]?token",
        r"private[_-]?key",
        r"backup[_-]?code",
        r"database[_-]?password",
        r"raw[_-]?user[_-]?secret",
        r"hmac[_-]?key",
        r"anchor[_-]?signing[_-]?key",
        r"signing_secret",
    )
]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_value(args: list[str]) -> str:
    proc = subprocess.run(["git", *args], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return (proc.stdout or "").strip()


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_load_error": str(exc)}


def scan_for_secrets(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    findings = []
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            findings.append({"pattern": pattern.pattern})
    return findings


def artifact_record(path: Path) -> dict:
    payload = load_json(path)
    chain = payload.get("chain") if isinstance(payload.get("chain"), dict) else {}
    return {
        "file_path": str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path),
        "exists": path.exists(),
        "sha256": sha256_file(path) if path.exists() else "",
        "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z") if path.exists() else "",
        "gate_status": payload.get("ok"),
        "release_candidate": payload.get("release_candidate", ""),
        "runtime": "54343" if "54343" in path.name else ("54344" if "54344" in path.name else ("synthetic" if "restore_drill" in path.name else "")),
        "chain_height": chain.get("latest_block_height"),
        "latest_block_hash": chain.get("latest_block_hash", ""),
        "chain_root": chain.get("chain_root", ""),
        "chain_verify": ((payload.get("verification") or {}).get("local_chain_verify") or {}).get("status", ""),
        "secret_scan_findings": scan_for_secrets(path) if path.exists() else [{"pattern": "missing_artifact"}],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create RC1.1-A artifact manifest.")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("artifacts", nargs="*", help="Artifact paths. Defaults to RC1.1-A known artifacts.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    paths = [Path(p) for p in args.artifacts] if args.artifacts else DEFAULT_ARTIFACTS
    records = [artifact_record(path if path.is_absolute() else ROOT / path) for path in paths]
    secret_findings = [
        {"file_path": record["file_path"], "findings": record["secret_scan_findings"]}
        for record in records
        if record["secret_scan_findings"]
    ]
    missing = [record["file_path"] for record in records if not record["exists"]]
    payload = {
        "ok": not missing and not secret_findings,
        "release": "RC1.1-A Operational Integrity Drills",
        "generated_at": utc_now(),
        "branch": git_value(["branch", "--show-current"]),
        "commit": git_value(["rev-parse", "--short", "HEAD"]),
        "artifacts": records,
        "missing_artifacts": missing,
        "secret_scan": {
            "ok": not secret_findings,
            "forbidden_patterns": [pattern.pattern for pattern in SECRET_PATTERNS],
            "findings": secret_findings,
        },
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": payload["ok"],
        "out": str(out),
        "artifact_count": len(records),
        "secret_scan": payload["secret_scan"]["ok"],
        "missing": missing,
    }, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
