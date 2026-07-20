#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
AUTO_RUN_ROOT=0
if [[ -n "${PYTEST_TMP_ROOT:-}" ]]; then
  RUN_ROOT="$(readlink -m -- "$PYTEST_TMP_ROOT")"
  case "$RUN_ROOT" in
    "$SOURCE_ROOT"|"$SOURCE_ROOT"/*)
      echo "[pytest-in-tmp] ERROR: PYTEST_TMP_ROOT must stay outside the source checkout" >&2
      exit 2
      ;;
  esac
  case "$RUN_ROOT" in
    /tmp/*) ;;
    *)
      echo "[pytest-in-tmp] ERROR: PYTEST_TMP_ROOT must resolve below /tmp" >&2
      exit 2
      ;;
  esac
  mkdir -p "$RUN_ROOT"
else
  RUN_ROOT="$(mktemp -d /tmp/hackme_web_pytest_XXXXXX)"
  AUTO_RUN_ROOT=1
fi
COPY_ROOT="$RUN_ROOT/hackme_web"
KEEP_TMP="${KEEP_TMP:-0}"

if [[ -e "$COPY_ROOT" || -L "$COPY_ROOT" ]]; then
  echo "[pytest-in-tmp] ERROR: copy target already exists: $COPY_ROOT" >&2
  exit 2
fi
mkdir "$COPY_ROOT"

tar -C "$SOURCE_ROOT" \
  --exclude='./.git' \
  --exclude='./.pytest_cache' \
  --exclude='./.venv' \
  --exclude='./__pycache__' \
  --exclude='./cache' \
  --exclude='./runtime' \
  --exclude='./docs/AGENTS/reports' \
  --exclude='./output' \
  --exclude='./public/generated' \
  --exclude='./playwright-report' \
  --exclude='./test-results' \
  --exclude='*/.pytest_cache' \
  --exclude='*/__pycache__' \
  --exclude='*/cache' \
  --exclude='*.pyc' \
  -cf - . | tar -C "$COPY_ROOT" -xf -

cd "$COPY_ROOT"
export HACKME_RUNTIME_DIR="$RUN_ROOT/runtime"
export HACKME_TEST_OUTPUT_ROOT="$RUN_ROOT/test_artifacts"
export TMPDIR="$RUN_ROOT/tmp"
mkdir -p "$HACKME_RUNTIME_DIR" "$HACKME_TEST_OUTPUT_ROOT" "$TMPDIR"
export PYTHONPATH="$COPY_ROOT"
export PYTHONPYCACHEPREFIX="$HACKME_RUNTIME_DIR/pycache"
export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -o cache_dir=$HACKME_RUNTIME_DIR/pytest_cache"

echo "[pytest-in-tmp] repo copy: $COPY_ROOT"
echo "[pytest-in-tmp] runtime:   $HACKME_RUNTIME_DIR"
echo "[pytest-in-tmp] running:   python3 -m pytest $*"

set +e
python3 -m pytest "$@"
status=$?
set -e
echo "[pytest-in-tmp] exit code: $status"

if [[ "$status" == "0" && "$KEEP_TMP" != "1" && "$AUTO_RUN_ROOT" == "1" ]]; then
  echo "[pytest-in-tmp] cleanup:   removing $RUN_ROOT"
  # Formal campaign tests deliberately seal evidence read-only. Restore only
  # this wrapper-owned temporary tree before deleting it so a green pytest run
  # cannot be turned into a false failure by cleanup permissions.
  find "$RUN_ROOT" -depth ! -type l -exec chmod u+rwX -- {} + 2>/dev/null || true
  rm -rf -- "$RUN_ROOT"
elif [[ "$status" == "0" && "$AUTO_RUN_ROOT" != "1" ]]; then
  echo "[pytest-in-tmp] kept caller-selected tmp root: $COPY_ROOT"
else
  echo "[pytest-in-tmp] kept tmp copy for debug: $COPY_ROOT"
fi

exit "$status"
