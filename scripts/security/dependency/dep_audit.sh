#!/usr/bin/env bash
# Dependency CVE and conflict audit for hackme_web.
#
# Tries in order: pip-audit (preferred), safety, then pip check for conflicts.
# Writes a Markdown report outside the source checkout.
#
# Usage:
#   scripts/security/dependency/dep_audit.sh [--out DIR] [--fail-on-vuln]
#
# Options:
#   --out DIR          Output directory for reports (default: /tmp/hackme_web_test_artifacts/reports/security)
#   --fail-on-vuln     Exit 1 if vulnerabilities found (default: exit 0, just report)
#   --pip-audit-args   Extra args forwarded to pip-audit
#   --safety-args      Extra args forwarded to safety
set -Eeuo pipefail

FAIL_ON_VULN=0
EXTRA_PIP_AUDIT_ARGS=()
EXTRA_SAFETY_ARGS=()

usage() {
  cat <<'USAGE'
Usage:
  scripts/security/dependency/dep_audit.sh [--out DIR] [--fail-on-vuln]

Options:
  --out DIR          Write reports under DIR (default: /tmp/hackme_web_test_artifacts/reports/security)
  --fail-on-vuln     Exit 1 if vulnerabilities or conflicts found
  --pip-audit-args   Quoted extra args for pip-audit (e.g. "--ignore-vuln PYSEC-2024-XXX")
  --safety-args      Quoted extra args for safety

Tools used (first available wins for vuln scan):
  pip-audit  — https://github.com/pypa/pip-audit
  safety     — https://github.com/pyupio/safety
  pip check  — always run for dependency conflict detection
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out)        REPORT_ROOT="${2:?missing dir}"; shift 2 ;;
    --fail-on-vuln) FAIL_ON_VULN=1; shift ;;
    --pip-audit-args) read -ra EXTRA_PIP_AUDIT_ARGS <<< "${2:-}"; shift 2 ;;
    --safety-args)   read -ra EXTRA_SAFETY_ARGS <<< "${2:-}"; shift 2 ;;
    -h|--help)    usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$REPO_ROOT"

default_report_root() {
  if [[ -n "${HTML_LEARNING_REPORTS_DIR:-}" ]]; then
    printf '%s/security' "${HTML_LEARNING_REPORTS_DIR%/}"
    return
  fi
  if [[ -n "${HACKME_RUNTIME_DIR:-}" ]]; then
    printf '%s/reports/security' "${HACKME_RUNTIME_DIR%/}"
    return
  fi
  printf '%s/reports/security' "${HACKME_TEST_OUTPUT_ROOT:-/tmp/hackme_web_test_artifacts}"
}

REPORT_ROOT="${REPORT_ROOT:-$(default_report_root)}"
REPORT_ROOT="$(readlink -m -- "$REPORT_ROOT")"
case "$REPORT_ROOT" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "ERROR: dependency audit reports must stay outside the source checkout" >&2
    exit 2
    ;;
esac

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)_${BASHPID}"
OUT_DIR="$REPORT_ROOT/dep_audit_${RUN_ID}"
mkdir -p "$OUT_DIR"

SUMMARY="$OUT_DIR/dep_audit.md"
VULN_FOUND=0
CONFLICT_FOUND=0

have() { command -v "$1" >/dev/null 2>&1; }

cat > "$SUMMARY" <<EOF
# Dependency Audit Report

- **repo**: \`$REPO_ROOT\`
- **run_id**: \`$RUN_ID\`
- **python**: \`$(python3 --version 2>&1)\`

EOF

echo "[*] Dependency CVE audit — $OUT_DIR"

# ── pip check (conflicts) ──────────────────────────────────────────────────────
echo "[*] pip check (dependency conflicts)"
PIP_CHECK_OUT="$OUT_DIR/pip_check.txt"
set +e
python3 -m pip check > "$PIP_CHECK_OUT" 2>&1
PIP_CHECK_EXIT=$?
set -e

{
  echo "## pip check (dependency conflicts)"
  echo ""
  echo '```'
  cat "$PIP_CHECK_OUT"
  echo '```'
  echo ""
} >> "$SUMMARY"

if [[ "$PIP_CHECK_EXIT" -ne 0 ]]; then
  echo "[!] pip check found conflicts — see $PIP_CHECK_OUT"
  CONFLICT_FOUND=1
else
  echo "[+] pip check: no conflicts"
fi

# ── Vulnerability scan ────────────────────────────────────────────────────────
VULN_OUT="$OUT_DIR/vuln_scan.txt"
SCAN_TOOL=""

if have pip-audit; then
  SCAN_TOOL="pip-audit"
  echo "[*] Running pip-audit …"
  set +e
  pip-audit --format=markdown "${EXTRA_PIP_AUDIT_ARGS[@]}" > "$VULN_OUT" 2>&1
  SCAN_EXIT=$?
  set -e
elif have safety; then
  SCAN_TOOL="safety"
  echo "[*] Running safety check …"
  set +e
  safety check --full-report "${EXTRA_SAFETY_ARGS[@]}" > "$VULN_OUT" 2>&1
  SCAN_EXIT=$?
  set -e
else
  SCAN_TOOL="none"
  SCAN_EXIT=0
  echo "[-] Neither pip-audit nor safety installed — skipping CVE scan" | tee "$VULN_OUT"
  echo "    Install: pip install pip-audit  (preferred)"
fi

{
  echo "## Vulnerability scan (tool: $SCAN_TOOL)"
  echo ""
  if [[ "$SCAN_TOOL" == "none" ]]; then
    echo "- **status**: skipped — install \`pip-audit\` or \`safety\`"
  elif [[ "$SCAN_EXIT" -eq 0 ]]; then
    echo "- **status**: ok (exit 0)"
  else
    echo "- **status**: issues found (exit $SCAN_EXIT)"
  fi
  echo ""
  if [[ -s "$VULN_OUT" ]]; then
    echo '```'
    cat "$VULN_OUT"
    echo '```'
  fi
  echo ""
} >> "$SUMMARY"

if [[ "$SCAN_EXIT" -ne 0 && "$SCAN_TOOL" != "none" ]]; then
  echo "[!] $SCAN_TOOL found vulnerabilities — see $VULN_OUT"
  VULN_FOUND=1
fi

# ── Installed package snapshot ────────────────────────────────────────────────
echo "[*] Snapshotting installed packages"
PKG_LIST="$OUT_DIR/installed_packages.txt"
python3 -m pip list --format=columns > "$PKG_LIST" 2>&1

{
  echo "## Installed packages snapshot"
  echo ""
  echo '```'
  cat "$PKG_LIST"
  echo '```'
  echo ""
} >> "$SUMMARY"

# ── Summary ───────────────────────────────────────────────────────────────────
{
  echo "## Overall"
  echo ""
  echo "| check | result |"
  echo "|-------|--------|"
  echo "| pip check (conflicts) | $([ "$CONFLICT_FOUND" -eq 0 ] && echo 'ok' || echo 'FAILED') |"
  echo "| $SCAN_TOOL CVE scan | $([ "$VULN_FOUND" -eq 0 ] && echo 'ok' || echo 'FAILED') |"
  echo ""
} >> "$SUMMARY"

echo "[*] Report: $SUMMARY"

if [[ "$FAIL_ON_VULN" -eq 1 && ( "$VULN_FOUND" -ne 0 || "$CONFLICT_FOUND" -ne 0 ) ]]; then
  echo "[!] Exiting 1 (--fail-on-vuln set)"
  exit 1
fi

exit 0
