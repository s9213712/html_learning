#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
RUNTIME_BASE="${PLAYWRIGHT_RUNTIME_BASE:-}"
AUTO_RUNTIME_BASE=0
KEEP_RUNTIME="${KEEP_PLAYWRIGHT_RUNTIME:-0}"
ACCEPTANCE_RETRIES="${PLAYWRIGHT_ACCEPTANCE_RETRIES:-1}"

if [[ -z "${RUNTIME_BASE}" ]]; then
  RUNTIME_BASE="$(mktemp -d /tmp/hackme_web_playwright_acceptance_XXXXXX)"
  AUTO_RUNTIME_BASE=1
else
  RUNTIME_BASE="$(readlink -m -- "$RUNTIME_BASE")"
  case "$RUNTIME_BASE" in
    /tmp/*) ;;
    *)
      echo "[playwright] ERROR: PLAYWRIGHT_RUNTIME_BASE must resolve below /tmp" >&2
      exit 2
      ;;
  esac
  if [[ -e "$RUNTIME_BASE" || -L "$RUNTIME_BASE" ]]; then
    echo "[playwright] ERROR: caller-selected runtime base must not already exist: $RUNTIME_BASE" >&2
    exit 2
  fi
  mkdir "$RUNTIME_BASE"
fi

cleanup() {
  if [[ "${KEEP_RUNTIME}" != "1" && "${AUTO_RUNTIME_BASE}" == "1" ]]; then
    rm -rf "${RUNTIME_BASE}"
  elif [[ "${AUTO_RUNTIME_BASE}" != "1" ]]; then
    echo "[playwright] kept caller-selected runtime base: ${RUNTIME_BASE}" >&2
  fi
}
trap cleanup EXIT

echo "[playwright] runtime base: ${RUNTIME_BASE}"

run_with_retry() {
  local label="$1"
  shift
  local attempts
  attempts="${ACCEPTANCE_RETRIES}"
  if ! [[ "${attempts}" =~ ^[0-9]+$ ]] || [[ "${attempts}" -lt 1 ]]; then
    attempts=1
  fi
  local attempt
  for attempt in $(seq 1 "${attempts}"); do
    echo "[playwright] ${label} attempt ${attempt}/${attempts}"
    if "$@" "${attempt}"; then
      return 0
    fi
    if [[ "${attempt}" -lt "${attempts}" ]]; then
      echo "[playwright] ${label} failed; retrying with a fresh isolated runtime"
    fi
  done
  return 1
}

run_workflow_builder() {
  python3 "${ROOT_DIR}/scripts/testing/playwright_comfyui_workflow_builder_check.py"
}

run_platform_health() {
  local attempt="$1"
  python3 "${ROOT_DIR}/scripts/testing/playwright_platform_health_check.py" \
    --runtime-root "${RUNTIME_BASE}/platform_attempt_${attempt}"
}

run_deep_flow() {
  local attempt="$1"
  python3 "${ROOT_DIR}/scripts/testing/playwright_deep_site_check.py" \
    --runtime-root "${RUNTIME_BASE}/deep_attempt_${attempt}" \
    --max-chess-human-moves "${MAX_CHESS_HUMAN_MOVES:-6}"
}

echo "[playwright] checking ComfyUI visual workflow builder"
run_with_retry "ComfyUI visual workflow builder" run_workflow_builder

echo "[playwright] checking platform center health"
run_with_retry "platform center health" run_platform_health

if [[ "${RUN_DEEP_PLAYWRIGHT:-0}" == "1" ]]; then
  echo "[playwright] checking deep site flow"
  run_with_retry "deep site flow" run_deep_flow
else
  echo "[playwright] deep site flow skipped; set RUN_DEEP_PLAYWRIGHT=1 to enable it"
fi

echo "[playwright] acceptance checks passed"
