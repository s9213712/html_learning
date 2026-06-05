#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_GIT_REPO_DIR="$SOURCE_ROOT"
CAPACITY_DEFAULTS_FILE="${HACKME_DEV_CAPACITY_DEFAULTS_FILE:-$SOURCE_ROOT/.hackme_capacity_defaults.env}"
CAPACITY_REPORT_DEFAULTS_FILE="${HACKME_DEV_CAPACITY_REPORT_FILE:-$SOURCE_ROOT/.hackme_capacity_report.json}"
CLOUD_DRIVE_STORAGE_ROOT="${HACKME_DEV_CLOUD_DRIVE_STORAGE_ROOT:-}"
CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB="${HACKME_DEV_CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB:-}"
MAX_CONTENT_MB="${HACKME_DEV_MAX_CONTENT_MB:-${HTML_LEARNING_MAX_CONTENT_MB:-}}"
RUN_ID="$(date +%Y%m%d_%H%M%S)"
RUN_ROOT=""
CUSTOM_RUNTIME_ROOT="${HACKME_DEV_RUNTIME_ROOT:-}"
CUSTOM_RUNTIME_ROOT_PROMPTED=0
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5000}"
TRUSTED_HOSTS="${HTML_LEARNING_TRUSTED_HOSTS:-}"
PUBLIC_HOST="${HACKME_DEV_PUBLIC_HOST:-}"
DISABLE_TRUSTED_HOSTS="${HACKME_DEV_DISABLE_TRUSTED_HOSTS:-${HTML_LEARNING_DISABLE_TRUSTED_HOSTS:-0}}"
SHUTDOWN=0
CLI_MODE=0
SKIP_INSTALL=0
FOREGROUND=0
IN_PLACE="${HACKME_DEV_IN_PLACE:-0}"
RUNTIME_IN_SOURCE="${HACKME_DEV_RUNTIME_IN_SOURCE:-${HACKME_DEV_DEPLOY_IN_PLACE:-0}}"
ROOT_PASSWORD="${ROOT_PASSWORD:-root}"
MANAGER_PASSWORD="${MANAGER_PASSWORD:-admin}"
TEST_PASSWORD="${TEST_PASSWORD:-test}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REQUIREMENTS_FILE="${HACKME_DEV_REQUIREMENTS_FILE:-requirements.txt}"
REQUIREMENTS_FILE_SET=0
[[ -n "${HACKME_DEV_REQUIREMENTS_FILE:-}" ]] && REQUIREMENTS_FILE_SET=1
FEATURE_MODE="${HACKME_DEV_FEATURE_MODE:-all}"
FEATURE_LIST="${HACKME_DEV_FEATURES:-}"
FEATURE_BUNDLES="${HACKME_DEV_FEATURE_BUNDLES:-${HACKME_DEV_FEATURE_PACKAGES:-}}"
FEATURE_LIST_FINALIZED=0
DEV_TOKEN_FEATURES="${HACKME_DEV_TOKEN_FEATURES:-${HACKME_DEV_INTERNAL_TEST_TOKEN_FEATURES:-}}"
DEV_TOKEN_TTL_MINUTES="${HACKME_DEV_TOKEN_TTL_MINUTES:-1440}"
DEV_TOKEN_USER="${HACKME_DEV_TOKEN_USER:-test}"
DEV_TOKEN_PASSWORD="${HACKME_DEV_TOKEN_PASSWORD:-}"
DEV_TOKEN_ROLE="${HACKME_DEV_TOKEN_ROLE:-user}"
FEATURE_MODE_SET=0
SECURITY_SETTINGS_ENABLED="${HACKME_DEV_SECURITY_ENABLED:-0}"
SESSION_IDLE_TIMEOUT_MINUTES="${HACKME_DEV_SESSION_IDLE_TIMEOUT_MINUTES:-}"
SERVER_MODE="${HACKME_DEV_SERVER_MODE:-dev_ready}"
EXTRA_ACCOUNTS="${HACKME_DEV_EXTRA_ACCOUNTS:-}"
PORT_CONFLICT_ACTION="${HACKME_DEV_PORT_CONFLICT_ACTION:-}"
BTC_TRADE_AUTOSTART="${HACKME_DEV_BTC_TRADE_AUTOSTART:-0}"
BACKTEST_PROBE_ON_STARTUP="${HACKME_DEV_BACKTEST_PROBE_ON_STARTUP:-0}"
TRADING_BACKGROUND_DEV_READY="${HACKME_DEV_TRADING_BACKGROUND_DEV_READY:-0}"
SERVER_RUNNER="${HACKME_DEV_SERVER_RUNNER:-gunicorn}"
GUNICORN_WORKERS="${HACKME_DEV_GUNICORN_WORKERS:-auto}"
GUNICORN_THREADS="${HACKME_DEV_GUNICORN_THREADS:-auto}"
GUNICORN_TIMEOUT="${HACKME_DEV_GUNICORN_TIMEOUT:-20}"
GUNICORN_GRACEFUL_TIMEOUT="${HACKME_DEV_GUNICORN_GRACEFUL_TIMEOUT:-10}"
GUNICORN_KEEP_ALIVE="${HACKME_DEV_GUNICORN_KEEP_ALIVE:-2}"
GUNICORN_BACKLOG="${HACKME_DEV_GUNICORN_BACKLOG:-64}"
GUNICORN_MAX_REQUESTS="${HACKME_DEV_GUNICORN_MAX_REQUESTS:-10000}"
GUNICORN_MAX_REQUESTS_JITTER="${HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER:-1000}"
CAPACITY_PROBE_MODE="${HACKME_DEV_CAPACITY_PROBE:-auto}"
CAPACITY_PROBE_TIER="${HACKME_DEV_CAPACITY_PROBE_TIER:-auto}"
CAPACITY_PROBE_RAN=0
CAPACITY_PROBE_REPORT_FILE=""
CAPACITY_REPORT_DEFAULTS_LOADED=0
CAPACITY_SETTINGS_FINALIZED=0
HLS_SLOT_PROBE_MODE="${HACKME_DEV_HLS_SLOT_PROBE:-ask}"
HLS_SLOT_PROBE_RAN=0
HLS_SLOT_PROBE_REPORT_FILE=""
BT_DOWNLOAD_BACKEND="${HACKME_BT_BACKEND:-auto}"
TRANSMISSION_RPC_URL="${HACKME_TRANSMISSION_RPC_URL:-http://127.0.0.1:9091/transmission/rpc}"
TRANSMISSION_RPC_USERNAME="${HACKME_TRANSMISSION_RPC_USERNAME:-}"
TRANSMISSION_RPC_PASSWORD="${HACKME_TRANSMISSION_RPC_PASSWORD:-}"
SETUP_TRANSMISSION_BACKEND="${HACKME_DEV_SETUP_TRANSMISSION_BACKEND:-0}"
TRANSMISSION_SETUP_SCRIPT="${HACKME_DEV_TRANSMISSION_SETUP_SCRIPT:-$SOURCE_ROOT/scripts/storage/setup_transmission_backend.sh}"
TRANSMISSION_SETUP_SERVICE="${HACKME_DEV_TRANSMISSION_SERVICE:-transmission-daemon}"
TRANSMISSION_SETUP_SETTINGS_FILE="${HACKME_DEV_TRANSMISSION_SETTINGS_FILE:-/etc/transmission-daemon/settings.json}"
TRANSMISSION_SETUP_RPC_BIND_ADDRESS="${HACKME_DEV_TRANSMISSION_RPC_BIND_ADDRESS:-}"
TRANSMISSION_SETUP_RPC_WHITELIST="${HACKME_DEV_TRANSMISSION_RPC_WHITELIST:-}"
TRANSMISSION_SETUP_RPC_WHITELIST_ENABLED="${HACKME_DEV_TRANSMISSION_RPC_WHITELIST_ENABLED:-}"
TRANSMISSION_SETUP_RPC_AUTHENTICATION_REQUIRED="${HACKME_DEV_TRANSMISSION_RPC_AUTHENTICATION_REQUIRED:-}"
TRANSMISSION_SETUP_ALLOW_ANY_RPC_IP="${HACKME_DEV_TRANSMISSION_ALLOW_ANY_RPC_IP:-0}"
BT_DOWNLOAD_STAGING_DIR="${HACKME_BT_DOWNLOAD_STAGING_DIR:-}"
BT_DOWNLOAD_CONFIG_SET=0
TRANSMISSION_CONFIG_SET=0
REMOTE_DOWNLOAD_LIMITS_SET=0
[[ -n "${HACKME_BT_BACKEND+x}" ]] && BT_DOWNLOAD_CONFIG_SET=1
[[ -n "${HACKME_TRANSMISSION_RPC_URL+x}" || -n "${HACKME_TRANSMISSION_RPC_USERNAME+x}" || -n "${HACKME_TRANSMISSION_RPC_PASSWORD+x}" || -n "${HACKME_BT_DOWNLOAD_STAGING_DIR+x}" || -n "${HACKME_DEV_SETUP_TRANSMISSION_BACKEND+x}" || -n "${HACKME_DEV_TRANSMISSION_SETUP_SCRIPT+x}" || -n "${HACKME_DEV_TRANSMISSION_SERVICE+x}" || -n "${HACKME_DEV_TRANSMISSION_SETTINGS_FILE+x}" || -n "${HACKME_DEV_TRANSMISSION_RPC_BIND_ADDRESS+x}" || -n "${HACKME_DEV_TRANSMISSION_RPC_WHITELIST+x}" || -n "${HACKME_DEV_TRANSMISSION_RPC_WHITELIST_ENABLED+x}" || -n "${HACKME_DEV_TRANSMISSION_RPC_AUTHENTICATION_REQUIRED+x}" || -n "${HACKME_DEV_TRANSMISSION_ALLOW_ANY_RPC_IP+x}" ]] && TRANSMISSION_CONFIG_SET=1
[[ -n "${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL+x}" || -n "${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER+x}" ]] && REMOTE_DOWNLOAD_LIMITS_SET=1
if [[ -z "${HACKME_DEV_COMFYUI_BASE_DIR:-}" && -d "/mnt/d/share/ComfyUI/models" ]]; then
  export HACKME_DEV_COMFYUI_BASE_DIR="/mnt/d/share/ComfyUI"
fi
DRY_RUN=0
BACKUP_RUNTIME=0
BACKUP_ARCHIVE=""
RESTORE_ARCHIVE=""
RESET_RUNTIME=0
DELETE_RUNTIME=0
RUN_ROOT_SET=0
RUNTIME_LAYOUT_SET=0
RUNTIME_ROOT_SET=0
RESTART_SCRIPT_FILE="${HACKME_DEV_RESTART_SCRIPT_FILE:-$SOURCE_ROOT/restart_develop_server.sh}"

is_auto_capacity_value() {
  local value="${1:-}"
  value="${value,,}"
  [[ -z "$value" || "$value" == "auto" || "$value" == "dynamic" || "$value" == "probe" ]]
}

gunicorn_capacity_auto_requested() {
  [[ "$SERVER_RUNNER" == "gunicorn" ]] || return 1
  is_auto_capacity_value "$GUNICORN_WORKERS" || is_auto_capacity_value "$GUNICORN_THREADS"
}

shell_quote() {
  printf '%q' "$1"
}

append_arg_if_value() {
  local target_var="$1"
  local option="$2"
  local value="$3"
  [[ -n "$value" ]] || return 0
  local -n target_args="$target_var"
  target_args+=("$option" "$value")
}

runtime_maintenance_action_requested() {
  [[ "$BACKUP_RUNTIME" == "1" || -n "$RESTORE_ARCHIVE" || "$RESET_RUNTIME" == "1" || "$DELETE_RUNTIME" == "1" ]]
}

ensure_single_runtime_maintenance_action() {
  local action_count=0
  [[ "$BACKUP_RUNTIME" == "1" ]] && action_count=$((action_count + 1))
  [[ -n "$RESTORE_ARCHIVE" ]] && action_count=$((action_count + 1))
  [[ "$RESET_RUNTIME" == "1" ]] && action_count=$((action_count + 1))
  [[ "$DELETE_RUNTIME" == "1" ]] && action_count=$((action_count + 1))
  if (( action_count != 1 )); then
    die "choose exactly one of --backup, --restore, --reset, or --delete"
  fi
}

normalize_runtime_maintenance_options() {
  normalize_cloud_drive_options
  normalize_custom_runtime_root
  if [[ "$RUNTIME_LAYOUT_SET" == "0" && "$RUNTIME_ROOT_SET" == "0" && "$RUN_ROOT_SET" == "0" ]]; then
    IN_PLACE=1
    RUNTIME_IN_SOURCE=1
  fi
  normalize_yes_no_value "$IN_PLACE" "in-place"
  IN_PLACE="$NORMALIZED_YES_NO"
  normalize_yes_no_value "$RUNTIME_IN_SOURCE" "runtime in source"
  RUNTIME_IN_SOURCE="$NORMALIZED_YES_NO"
  if [[ "$RUNTIME_IN_SOURCE" == "1" ]]; then
    IN_PLACE=1
  fi
}

write_restart_shortcut_script() {
  local shortcut_path="${RESTART_SCRIPT_FILE:-}"
  [[ -n "$shortcut_path" ]] || return 0
  local launcher="$SOURCE_ROOT/test_for_develop.sh"
  [[ -f "$launcher" ]] || return 0

  local restart_args=(
    --cli
    --host "$HOST"
    --port "$PORT"
    --feature-mode "$FEATURE_MODE"
    --server-mode "$SERVER_MODE"
    --port-conflict "$PORT_CONFLICT_ACTION"
    --server-runner "$SERVER_RUNNER"
    --capacity-probe-tier "$CAPACITY_PROBE_TIER"
    --bt-backend "$BT_DOWNLOAD_BACKEND"
    --transmission-rpc-url "$TRANSMISSION_RPC_URL"
    --security "$SECURITY_SETTINGS_ENABLED"
    --session-idle-timeout-minutes "$SESSION_IDLE_TIMEOUT_MINUTES"
    --token-ttl-minutes "$DEV_TOKEN_TTL_MINUTES"
    --token-user "$DEV_TOKEN_USER"
    --token-role "$DEV_TOKEN_ROLE"
    --requirements-file "$REQUIREMENTS_FILE"
    --root-password "$ROOT_PASSWORD"
    --manager-password "$MANAGER_PASSWORD"
    --test-password "$TEST_PASSWORD"
  )

  append_arg_if_value restart_args --trusted-hosts "$TRUSTED_HOSTS"
  append_arg_if_value restart_args --public-host "$PUBLIC_HOST"
  append_arg_if_value restart_args --feature-bundles "$FEATURE_BUNDLES"
  append_arg_if_value restart_args --features "$FEATURE_LIST"
  append_arg_if_value restart_args --token-features "$DEV_TOKEN_FEATURES"
  append_arg_if_value restart_args --token-password "$DEV_TOKEN_PASSWORD"
  append_arg_if_value restart_args --accounts "$EXTRA_ACCOUNTS"
  append_arg_if_value restart_args --capacity-defaults-file "$CAPACITY_DEFAULTS_FILE"
  append_arg_if_value restart_args --capacity-report-file "$CAPACITY_REPORT_DEFAULTS_FILE"
  append_arg_if_value restart_args --cloud-drive-storage-root "$CLOUD_DRIVE_STORAGE_ROOT"
  append_arg_if_value restart_args --cloud-drive-global-capacity-limit-mb "$CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB"
  append_arg_if_value restart_args --max-content-mb "$MAX_CONTENT_MB"
  append_arg_if_value restart_args --runtime-root "$CUSTOM_RUNTIME_ROOT"
  append_arg_if_value restart_args --transmission-rpc-username "$TRANSMISSION_RPC_USERNAME"
  append_arg_if_value restart_args --transmission-rpc-password "$TRANSMISSION_RPC_PASSWORD"
  append_arg_if_value restart_args --transmission-setup-script "$TRANSMISSION_SETUP_SCRIPT"
  append_arg_if_value restart_args --transmission-settings-file "$TRANSMISSION_SETUP_SETTINGS_FILE"
  append_arg_if_value restart_args --transmission-service "$TRANSMISSION_SETUP_SERVICE"
  append_arg_if_value restart_args --transmission-rpc-bind-address "$TRANSMISSION_SETUP_RPC_BIND_ADDRESS"
  append_arg_if_value restart_args --transmission-rpc-whitelist "$TRANSMISSION_SETUP_RPC_WHITELIST"
  append_arg_if_value restart_args --transmission-rpc-whitelist-enabled "$TRANSMISSION_SETUP_RPC_WHITELIST_ENABLED"
  append_arg_if_value restart_args --transmission-rpc-authentication-required "$TRANSMISSION_SETUP_RPC_AUTHENTICATION_REQUIRED"
  append_arg_if_value restart_args --bt-download-staging-dir "$BT_DOWNLOAD_STAGING_DIR"
  append_arg_if_value restart_args --remote-download-global "${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL:-}"
  append_arg_if_value restart_args --remote-download-per-user "${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER:-}"

  if [[ "$SETUP_TRANSMISSION_BACKEND" == "1" ]]; then
    restart_args+=(--setup-transmission-backend)
  else
    restart_args+=(--no-setup-transmission-backend)
  fi
  if [[ "$TRANSMISSION_SETUP_ALLOW_ANY_RPC_IP" == "1" ]]; then
    restart_args+=(--transmission-allow-any-rpc-ip)
  fi
  restart_args+=(--no-capacity-probe --no-hls-slot-probe)
  if [[ "$DISABLE_TRUSTED_HOSTS" == "1" ]]; then
    restart_args+=(--allow-any-host)
  fi
  if [[ "$RUNTIME_IN_SOURCE" == "1" ]]; then
    restart_args+=(--runtime-in-source)
  elif [[ "$IN_PLACE" == "1" ]]; then
    restart_args+=(--in-place)
  else
    restart_args+=(--copy)
  fi
  if [[ "$SKIP_INSTALL" == "1" ]]; then
    restart_args+=(--skip-install)
  fi
  if [[ "$FOREGROUND" == "1" ]]; then
    restart_args+=(--foreground)
  fi
  if [[ "$BTC_TRADE_AUTOSTART" == "1" ]]; then
    restart_args+=(--btc-trade-autostart)
  else
    restart_args+=(--no-btc-trade-autostart)
  fi
  if [[ "$BACKTEST_PROBE_ON_STARTUP" == "1" ]]; then
    restart_args+=(--backtest-probe-on-startup)
  fi
  if [[ "$TRADING_BACKGROUND_DEV_READY" == "1" ]]; then
    restart_args+=(--trading-background-dev-ready)
  else
    restart_args+=(--no-trading-background-dev-ready)
  fi
  if [[ "$SERVER_RUNNER" == "gunicorn" ]]; then
    restart_args+=(
      --gunicorn-workers "$GUNICORN_WORKERS"
      --gunicorn-threads "$GUNICORN_THREADS"
      --gunicorn-timeout "$GUNICORN_TIMEOUT"
      --gunicorn-graceful-timeout "$GUNICORN_GRACEFUL_TIMEOUT"
      --gunicorn-keep-alive "$GUNICORN_KEEP_ALIVE"
      --gunicorn-backlog "$GUNICORN_BACKLOG"
      --gunicorn-max-requests "$GUNICORN_MAX_REQUESTS"
      --gunicorn-max-requests-jitter "$GUNICORN_MAX_REQUESTS_JITTER"
    )
  fi

  mkdir -p "$(dirname "$shortcut_path")"
  {
    printf '%s\n' '#!/usr/bin/env bash'
    printf '%s\n' 'set -Eeuo pipefail'
    local env_name env_value
    local restart_env_names=(
      HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY
      HACKME_MEDIA_HLS_MAX_CONCURRENT
      HACKME_MEDIA_HLS_SERIALIZE_ALL
      HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL
      HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER
      HACKME_BT_DOWNLOAD_STAGING_DIR
      HACKME_DEV_TRANSMISSION_RPC_AUTHENTICATION_REQUIRED
      HACKME_DEV_COMFYUI_CONNECTION_MODE
      HACKME_DEV_COMFYUI_REMOTE_API_URL
      HACKME_DEV_COMFYUI_BASE_DIR
      HACKME_DEV_COMFYUI_LOCAL_START_SCRIPT
    )
    for env_name in "${restart_env_names[@]}"; do
      env_value="${!env_name:-}"
      [[ -n "$env_value" ]] || continue
      printf 'export %s=%s\n' "$env_name" "$(shell_quote "$env_value")"
    done
    printf 'cd %s\n' "$(shell_quote "$SOURCE_ROOT")"
    printf 'exec %s' "$(shell_quote "$launcher")"
    local arg
    for arg in "${restart_args[@]}"; do
      printf ' %s' "$(shell_quote "$arg")"
    done
    printf ' "$@"\n'
  } > "$shortcut_path"
  chmod 700 "$shortcut_path"
  say "[dev-tmp] shortcut: $(cd "$(dirname "$shortcut_path")" && pwd -P)/$(basename "$shortcut_path")"
}

load_local_capacity_defaults() {
  local mode="${1:-normal}"
  [[ "${HACKME_DEV_USE_CAPACITY_DEFAULTS:-1}" != "0" ]] || return 0
  [[ -f "$CAPACITY_DEFAULTS_FILE" ]] || return 0
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -n "$line" && "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    case "$key" in
      HACKME_DEV_GUNICORN_WORKERS)
        if [[ "$mode" == "force" ]] || is_auto_capacity_value "$GUNICORN_WORKERS"; then
          GUNICORN_WORKERS="$value"
          export HACKME_DEV_GUNICORN_WORKERS="$value"
        fi
        ;;
      HACKME_DEV_GUNICORN_THREADS)
        if [[ "$mode" == "force" ]] || is_auto_capacity_value "$GUNICORN_THREADS"; then
          GUNICORN_THREADS="$value"
          export HACKME_DEV_GUNICORN_THREADS="$value"
        fi
        ;;
      HACKME_DEV_GUNICORN_MAX_REQUESTS)
        if [[ "$mode" == "force" || -z "${HACKME_DEV_GUNICORN_MAX_REQUESTS+x}" ]]; then
          GUNICORN_MAX_REQUESTS="$value"
          export HACKME_DEV_GUNICORN_MAX_REQUESTS="$value"
        fi
        ;;
      HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER)
        if [[ "$mode" == "force" || -z "${HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER+x}" ]]; then
          GUNICORN_MAX_REQUESTS_JITTER="$value"
          export HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER="$value"
        fi
        ;;
      HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY)
        if [[ "$mode" == "force" || -z "${HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY+x}" || "$(printf '%s' "${HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY:-}" | tr '[:upper:]' '[:lower:]')" == "auto" ]]; then
          export HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY="$value"
        fi
        ;;
      HACKME_MEDIA_HLS_MAX_CONCURRENT)
        if [[ "$mode" == "force" || -z "${HACKME_MEDIA_HLS_MAX_CONCURRENT+x}" || "$(printf '%s' "${HACKME_MEDIA_HLS_MAX_CONCURRENT:-}" | tr '[:upper:]' '[:lower:]')" == "auto" ]]; then
          export HACKME_MEDIA_HLS_MAX_CONCURRENT="$value"
        fi
        ;;
      HACKME_MEDIA_HLS_SERIALIZE_ALL)
        if [[ "$mode" == "force" || -z "${HACKME_MEDIA_HLS_SERIALIZE_ALL+x}" || "$(printf '%s' "${HACKME_MEDIA_HLS_SERIALIZE_ALL:-}" | tr '[:upper:]' '[:lower:]')" == "auto" ]]; then
          export HACKME_MEDIA_HLS_SERIALIZE_ALL="$value"
        fi
        ;;
      HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL)
        if [[ "$mode" == "force" || -z "${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL+x}" || "$(printf '%s' "${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL:-}" | tr '[:upper:]' '[:lower:]')" == "auto" ]]; then
          export HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL="$value"
        fi
        ;;
      HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER)
        if [[ "$mode" == "force" || -z "${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER+x}" || "$(printf '%s' "${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER:-}" | tr '[:upper:]' '[:lower:]')" == "auto" ]]; then
          export HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER="$value"
        fi
        ;;
    esac
  done < "$CAPACITY_DEFAULTS_FILE"
}

load_local_capacity_report_defaults() {
  [[ "$CAPACITY_REPORT_DEFAULTS_LOADED" == "1" ]] && return 0
  [[ "${HACKME_DEV_USE_CAPACITY_DEFAULTS:-1}" != "0" ]] || return 1
  [[ -n "$CAPACITY_REPORT_DEFAULTS_FILE" && -s "$CAPACITY_REPORT_DEFAULTS_FILE" ]] || return 1
  load_capacity_probe_report_summary "$CAPACITY_REPORT_DEFAULTS_FILE" || return 1
  [[ "${CAPACITY_REPORT_OK:-0}" == "1" ]] || return 1
  CAPACITY_REPORT_DEFAULTS_LOADED=1
  say "[dev-tmp] capacity report: loaded $CAPACITY_REPORT_PROFILE from $CAPACITY_REPORT_DEFAULTS_FILE"
  return 0
}


load_capacity_env_defaults_preview() {
  CAPACITY_ENV_WORKERS=""
  CAPACITY_ENV_THREADS=""
  CAPACITY_ENV_MAX_REQUESTS=""
  CAPACITY_ENV_MAX_REQUESTS_JITTER=""
  CAPACITY_ENV_BACKPRESSURE=""
  CAPACITY_ENV_HLS_MAX_CONCURRENT=""
  CAPACITY_ENV_HLS_SERIALIZE_ALL=""
  CAPACITY_ENV_REMOTE_DOWNLOAD_GLOBAL=""
  CAPACITY_ENV_REMOTE_DOWNLOAD_PER_USER=""
  [[ -f "$CAPACITY_DEFAULTS_FILE" ]] || return 1
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -n "$line" && "$line" == *=* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    value="${value%\"}"
    value="${value#\"}"
    value="${value%\'}"
    value="${value#\'}"
    case "$key" in
      HACKME_DEV_GUNICORN_WORKERS) CAPACITY_ENV_WORKERS="$value" ;;
      HACKME_DEV_GUNICORN_THREADS) CAPACITY_ENV_THREADS="$value" ;;
      HACKME_DEV_GUNICORN_MAX_REQUESTS) CAPACITY_ENV_MAX_REQUESTS="$value" ;;
      HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER) CAPACITY_ENV_MAX_REQUESTS_JITTER="$value" ;;
      HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY) CAPACITY_ENV_BACKPRESSURE="$value" ;;
      HACKME_MEDIA_HLS_MAX_CONCURRENT) CAPACITY_ENV_HLS_MAX_CONCURRENT="$value" ;;
      HACKME_MEDIA_HLS_SERIALIZE_ALL) CAPACITY_ENV_HLS_SERIALIZE_ALL="$value" ;;
      HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL) CAPACITY_ENV_REMOTE_DOWNLOAD_GLOBAL="$value" ;;
      HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER) CAPACITY_ENV_REMOTE_DOWNLOAD_PER_USER="$value" ;;
    esac
  done < "$CAPACITY_DEFAULTS_FILE"
  [[ -n "$CAPACITY_ENV_WORKERS" || -n "$CAPACITY_ENV_THREADS" || -n "$CAPACITY_ENV_BACKPRESSURE" || -n "$CAPACITY_ENV_HLS_MAX_CONCURRENT" || -n "$CAPACITY_ENV_REMOTE_DOWNLOAD_GLOBAL" ]]
}

load_capacity_report_defaults_preview() {
  CAPACITY_JSON_OK=0
  CAPACITY_JSON_WORKERS=""
  CAPACITY_JSON_THREADS=""
  CAPACITY_JSON_MAX_REQUESTS=""
  CAPACITY_JSON_MAX_REQUESTS_JITTER=""
  CAPACITY_JSON_BACKPRESSURE=""
  CAPACITY_JSON_HLS_MAX_CONCURRENT=""
  CAPACITY_JSON_HLS_SERIALIZE_ALL=""
  CAPACITY_JSON_REMOTE_DOWNLOAD_GLOBAL=""
  CAPACITY_JSON_REMOTE_DOWNLOAD_PER_USER=""
  CAPACITY_JSON_PROFILE=""
  CAPACITY_JSON_ACCOUNTS=""
  CAPACITY_JSON_TARGET_P95=""
  CAPACITY_JSON_LAT_P50=""
  CAPACITY_JSON_LAT_P95=""
  CAPACITY_JSON_LAT_P99=""
  CAPACITY_JSON_LAT_MAX=""
  [[ -n "$CAPACITY_REPORT_DEFAULTS_FILE" && -s "$CAPACITY_REPORT_DEFAULTS_FILE" ]] || return 1
  local preview
  if ! preview="$($PYTHON_BIN - "$CAPACITY_REPORT_DEFAULTS_FILE" <<'REPORTPREVIEWPY'
import json
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    report = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print("CAPACITY_JSON_OK=0")
    print(f"CAPACITY_JSON_ERROR={shlex.quote(type(exc).__name__ + ': ' + str(exc))}")
    raise SystemExit(0)
recommendation = report.get("recommendation") or {}
suggested_env = recommendation.get("suggested_env") or {}
latency = recommendation.get("observed_latency_ms") or {}
workers = recommendation.get("workers") if recommendation.get("ok") else ""
threads = recommendation.get("threads") if recommendation.get("ok") else ""

def emit(key, value):
    if value is None:
        value = ""
    print(f"{key}={shlex.quote(str(value))}")

emit("CAPACITY_JSON_OK", "1" if recommendation.get("ok") else "0")
emit("CAPACITY_JSON_WORKERS", workers or "")
emit("CAPACITY_JSON_THREADS", threads or "")
emit("CAPACITY_JSON_MAX_REQUESTS", suggested_env.get("HACKME_DEV_GUNICORN_MAX_REQUESTS") or "")
emit("CAPACITY_JSON_MAX_REQUESTS_JITTER", suggested_env.get("HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER") or "")
emit("CAPACITY_JSON_BACKPRESSURE", suggested_env.get("HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY") or (max(4, int(threads or 0)) if threads else ""))
emit("CAPACITY_JSON_HLS_MAX_CONCURRENT", suggested_env.get("HACKME_MEDIA_HLS_MAX_CONCURRENT") or "")
emit("CAPACITY_JSON_HLS_SERIALIZE_ALL", suggested_env.get("HACKME_MEDIA_HLS_SERIALIZE_ALL") or "")
emit("CAPACITY_JSON_REMOTE_DOWNLOAD_GLOBAL", suggested_env.get("HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL") or "")
emit("CAPACITY_JSON_REMOTE_DOWNLOAD_PER_USER", suggested_env.get("HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER") or "")
emit("CAPACITY_JSON_PROFILE", f"{workers}x{threads}" if workers and threads else "")
emit("CAPACITY_JSON_ACCOUNTS", recommendation.get("max_passing_accounts") or "")
emit("CAPACITY_JSON_TARGET_P95", recommendation.get("target_p95_ms") or "")
emit("CAPACITY_JSON_LAT_P50", latency.get("p50") or "")
emit("CAPACITY_JSON_LAT_P95", latency.get("p95") or "")
emit("CAPACITY_JSON_LAT_P99", latency.get("p99") or "")
emit("CAPACITY_JSON_LAT_MAX", latency.get("max") or "")
REPORTPREVIEWPY
)"; then
    return 1
  fi
  eval "$preview"
  [[ "${CAPACITY_JSON_OK:-0}" == "1" ]]
}

print_capacity_defaults_candidate() {
  local label="$1"
  local source_path="$2"
  local workers="$3"
  local threads="$4"
  local backpressure="$5"
  local max_requests="$6"
  local jitter="$7"
  local hls_max="$8"
  local hls_serialize="$9"
  local remote_global="${10}"
  local remote_per_user="${11}"
  local extra="${12}"
  say "  $label"
  say "     file:        $source_path"
  say "     gunicorn:    workers=${workers:-?} threads=${threads:-?} max_requests=${max_requests:-?} jitter=${jitter:-?}"
  say "     backpressure:${backpressure:-?}"
  say "     hls:         max_concurrent=${hls_max:-?} serialize_all=${hls_serialize:-?}"
  say "     remote dl:   global=${remote_global:-?} per_user=${remote_per_user:-?}"
  if [[ -n "$extra" ]]; then
    say "     observed:    $extra"
  fi
}

prompt_capacity_defaults_source() {
  [[ "${HACKME_DEV_USE_CAPACITY_DEFAULTS:-1}" != "0" ]] || return 0
  if [[ "$CLI_MODE" == "1" || "$SHUTDOWN" == "1" ]]; then
    load_local_capacity_report_defaults || load_local_capacity_defaults
    return 0
  fi

  local has_json=0
  local has_env=0
  load_capacity_report_defaults_preview && has_json=1 || true
  load_capacity_env_defaults_preview && has_env=1 || true

  if [[ "$has_json" != "1" && "$has_env" != "1" ]]; then
    return 0
  fi

  say "Detected existing capacity/backpressure defaults. Choose how to use them for this launch:"
  if [[ "$has_json" == "1" ]]; then
    print_capacity_defaults_candidate \
      "1) JSON capacity report" \
      "$CAPACITY_REPORT_DEFAULTS_FILE" \
      "$CAPACITY_JSON_WORKERS" \
      "$CAPACITY_JSON_THREADS" \
      "$CAPACITY_JSON_BACKPRESSURE" \
      "$CAPACITY_JSON_MAX_REQUESTS" \
      "$CAPACITY_JSON_MAX_REQUESTS_JITTER" \
      "$CAPACITY_JSON_HLS_MAX_CONCURRENT" \
      "$CAPACITY_JSON_HLS_SERIALIZE_ALL" \
      "$CAPACITY_JSON_REMOTE_DOWNLOAD_GLOBAL" \
      "$CAPACITY_JSON_REMOTE_DOWNLOAD_PER_USER" \
      "safe_accounts=${CAPACITY_JSON_ACCOUNTS:-?} p50=${CAPACITY_JSON_LAT_P50:-?}ms p95=${CAPACITY_JSON_LAT_P95:-?}ms p99=${CAPACITY_JSON_LAT_P99:-?}ms max=${CAPACITY_JSON_LAT_MAX:-?}ms target_p95=${CAPACITY_JSON_TARGET_P95:-?}ms"
  else
    say "  1) JSON capacity report unavailable: $CAPACITY_REPORT_DEFAULTS_FILE"
  fi
  if [[ "$has_env" == "1" ]]; then
    print_capacity_defaults_candidate \
      "2) Env defaults" \
      "$CAPACITY_DEFAULTS_FILE" \
      "$CAPACITY_ENV_WORKERS" \
      "$CAPACITY_ENV_THREADS" \
      "$CAPACITY_ENV_BACKPRESSURE" \
      "$CAPACITY_ENV_MAX_REQUESTS" \
      "$CAPACITY_ENV_MAX_REQUESTS_JITTER" \
      "$CAPACITY_ENV_HLS_MAX_CONCURRENT" \
      "$CAPACITY_ENV_HLS_SERIALIZE_ALL" \
      "$CAPACITY_ENV_REMOTE_DOWNLOAD_GLOBAL" \
      "$CAPACITY_ENV_REMOTE_DOWNLOAD_PER_USER" \
      ""
  else
    say "  2) Env defaults unavailable: $CAPACITY_DEFAULTS_FILE"
  fi
  say "  3) retest capacity now"
  say "  4) enter manual capacity/backpressure settings"
  say "  5) conservative fallback"
  local choice
  while true; do
    printf 'Choose capacity defaults source [1]: '
    if ! read -r choice; then
      die "interactive setup was interrupted"
    fi
    choice="${choice:-1}"
    case "${choice,,}" in
      1|json|report)
        [[ "$has_json" == "1" ]] || { say "JSON capacity report is unavailable; choose another option."; continue; }
        load_local_capacity_report_defaults || die "selected JSON capacity report could not be loaded"
        CAPACITY_SETTINGS_FINALIZED=1
        return 0
        ;;
      2|env|defaults)
        [[ "$has_env" == "1" ]] || { say "Env defaults are unavailable; choose another option."; continue; }
        load_local_capacity_defaults force
        CAPACITY_SETTINGS_FINALIZED=1
        say "[dev-tmp] capacity defaults: loaded env defaults from $CAPACITY_DEFAULTS_FILE"
        return 0
        ;;
      3|retest|probe|rerun)
        CAPACITY_PROBE_MODE="force"
        if [[ "$CAPACITY_PROBE_TIER" == "auto" ]]; then
          prompt_capacity_probe_tier
        fi
        run_capacity_probe_for_defaults
        return 0
        ;;
      4|manual|custom)
        prompt_manual_capacity_settings
        return 0
        ;;
      5|fallback|conservative)
        reset_capacity_to_conservative_fallback
        return 0
        ;;
      *)
        say "Please choose 1, 2, 3, 4, or 5."
        ;;
    esac
  done
}

usage() {
  cat <<'USAGE'
Usage:
  ./test_for_develop.sh [options]

Purpose:
  Copy a lean runtime/development subset to /tmp by default, initialize a
  development-friendly runtime, and launch server.py from the copied workspace
  so the repo never accumulates runtime or cache pollution. Pass --in-place /
  --no-copy when you explicitly want to launch from the current repo without
  copying source files while still keeping runtime under --run-root. Pass
  --runtime-in-source / --deploy-in-place when you intentionally want the
  current repo to own ./runtime directly.

Important:
  Without --cli, the script asks for workspace, host, port, server runner,
  feature mode, security posture, server mode, dependency handling, foreground
  mode, BTC_trade autostart, account password settings, and extra accounts.
  With --cli, it never prompts and only uses command-line/env values.

  For server-mode / production-gate validation, HTML_LEARNING_GIT_REPO_DIR must
  point at a real git repo with a readable .git directory. The /tmp copy is
  intentionally source-only and excludes git metadata, docs, reference repos,
  deployment examples, non-runtime README files, generated runtime/cache data,
  and other non-runtime artifacts. Edit those files directly in the source repo.

Options:
  --cli                    Run non-interactively from command/env options
  --host HOST              Default: 127.0.0.1
  --port PORT              Default: 5000; prompts if occupied in interactive mode
  --trusted-hosts LIST     Comma-separated Host allowlist exported as
                           HTML_LEARNING_TRUSTED_HOSTS. Use this when exposing
                           the dev server through a LAN/public IP.
  --public-host HOST       Add HOST to trusted hosts and print it as an
                           external HTTPS test URL. Alias-friendly for NAT IPs.
  --allow-any-host         Development escape hatch: disable Flask trusted-host
                           checks for this launch. Do not use for production.
  --stop                   Stop prior dev server process group / child tree for
                           --port and exit. Only terminates processes launched
                           from hackme_web dev runtime paths or this source repo.
                           --shutdown remains accepted as a compatibility alias.
  --feature-mode MODE      all, defaults, bundles, or custom. Default: all
  --feature-bundles LIST   Comma-separated feature package names such as
                           ops-minimum,safe-community,creator-media,exchange-ops,ai.
  --features LIST          Comma-separated feature_* keys or package names for
                           custom mode. Required/recommended dependencies are
                           expanded automatically.
  --token-features LIST    Comma-separated feature_* keys, short feature names,
                           or interactive list numbers allowed by generated
                           test/internal-test dev tokens. Empty/0 means no extra
                           token-level feature restriction.
  --internal-test-token-features LIST
                           Comma-separated feature_* keys allowed by the
                           generated internal-test login token. Alias of
                           --token-features.
  --token-ttl-minutes N    TTL for generated test/internal-test tokens.
                           Default: 1440
  --token-user USERNAME    Account bound to generated test/internal-test tokens.
                           Default: test
  --token-password VALUE   Password to set when creating/updating --token-user.
                           Blank keeps existing users unchanged and auto-generates
                           a password only when the token user does not exist.
  --token-role ROLE        Role for a newly created/updated token account.
                           user, manager, or super_admin. Default: user
  --security VALUE         on/off. Default: off for dev-friendly runtime
  --session-idle-timeout-minutes N,
  --idle-timeout-minutes N,
  --logout-countdown-minutes N
                           Override the frontend idle logout countdown in
                           minutes for this dev runtime. 0 disables it; blank
                           keeps the selected security profile default
                           (dev-friendly: 1440, security-enabled: 60).
  --server-mode MODE       dev_ready, internal_test, test, preprod, production,
                           superweak, maintenance, or incident_lockdown
  --add-account SPEC       Add dev account as username:password[:role]; repeatable
  --accounts LIST          Comma-separated --add-account specs
  --port-conflict ACTION   ask, kill, fallback, or fail. Default: ask interactively,
                           fallback under --cli. kill falls back to another port
                           if the process cannot be terminated or the port stays busy
  --btc-trade-autostart    Start BTC_trade in the background after boot
  --no-btc-trade-autostart Do not start BTC_trade in the background
  --backtest-probe-on-startup
                           Run the first-boot trading backtest capacity probe
                           in this temporary runtime
  --trading-background-dev-ready
                           In dev_ready mode, allow trading background jobs
                           that mutate trading state: price refresh, matching,
                           bot scan, liquidation, and interest accrual
  --no-trading-background-dev-ready
                           Keep dev_ready trading background jobs disabled
                           except sitewide metrics refresh. Default
  --server-runner RUNNER    flask or gunicorn. Default: gunicorn
  --gunicorn-workers N      Default: auto when --server-runner gunicorn
                           auto means local capacity probe result when present;
                           otherwise the script runs one unless disabled.
  --gunicorn-threads N      Default: auto when --server-runner gunicorn
  --gunicorn-timeout N      Default: 20 seconds
  --gunicorn-backlog N      Default: 64
  --gunicorn-max-requests N Default: 10000; 0 disables worker recycling
  --capacity-probe          Run/refresh the local capacity probe before launch
  --capacity-probe-tier TIER
                           Hardware-sized capacity preset: auto, sbc, legacy,
                           laptop, midrange, or highend. sbc/legacy are safest
                           for single-board computers, old desktops, and fragile
                           hosts; highend is the full default search.
  --capacity-probe-light    Alias for --capacity-probe --capacity-probe-tier legacy
  --no-capacity-probe       Do not probe when auto has no local result; use the
                           conservative hardware fallback for this run
  --capacity-defaults-file PATH
                           Default: .hackme_capacity_defaults.env in repo root
  --capacity-report-file PATH
                           JSON capacity report to read before the env defaults.
                           Default: .hackme_capacity_report.json in repo root
  --hls-slot-probe         Run a quick Premium HLS slot sizing probe before launch
  --no-hls-slot-probe      Skip the HLS slot sizing startup test
  --bt-backend BACKEND     BT/magnet backend: auto, transmission, or aria2
  --transmission-rpc-url URL
                           Transmission RPC endpoint for BT/magnet downloads
  --transmission-rpc-username USER
  --transmission-rpc-password PASS
                           Optional Transmission RPC credentials
  --setup-transmission-backend
                           Run scripts/storage/setup_transmission_backend.sh
                           before app launch to configure daemon settings,
                           shared storage permissions, and BT staging dir.
                           Requires sudo/root; the helper remains the single
                           source of truth for Transmission daemon setup.
  --transmission-setup-script PATH
                           Override setup helper path. Default:
                           scripts/storage/setup_transmission_backend.sh
  --transmission-settings-file PATH
                           Transmission settings.json path passed to setup.
                           Default: /etc/transmission-daemon/settings.json
  --transmission-service NAME
                           systemd service passed to setup. Default:
                           transmission-daemon
  --transmission-rpc-bind-address ADDR
                           RPC bind address passed to setup helper. Default:
                           helper default 127.0.0.1
  --transmission-rpc-whitelist LIST
                           RPC IP whitelist passed to setup helper. Default:
                           helper default 127.0.0.1,::1
  --transmission-rpc-whitelist-enabled VALUE
                           Enable RPC IP whitelist in setup helper: true/false.
  --transmission-rpc-authentication-required VALUE
                           Require Transmission RPC/Web UI login in setup
                           helper: true/false.
  --transmission-disable-rpc-auth
                           Development-only: configure Transmission RPC/Web UI
                           without login. Use only on an isolated local network.
  --transmission-allow-any-rpc-ip
                           Configure daemon RPC to listen on 0.0.0.0 and allow
                           any source IP. Authentication is still required
                           unless --transmission-disable-rpc-auth is also set.
  --bt-download-staging-dir PATH,
  --transmission-download-dir PATH
                           Directory hackme_web scans/imports from when using a
                           manually configured Transmission daemon. Automatic
                           setup fills this from the helper output.
  --remote-download-global N
  --remote-download-per-user N
                           Remote download global/per-user concurrency defaults
  --cloud-drive-root PATH,
  --cloud-drive-storage-root PATH
                           Use PATH as the actual cloud-drive file storage
                           location instead of runtime/storage. Must be an
                           absolute, non-public, non-project-root path.
                           If the selected run-root already has files under
                           runtime/storage, missing files are copied into PATH
                           on startup so existing dev metadata remains readable.
  --cloud-drive-max-mb MB,
  --cloud-drive-global-capacity-limit-mb MB
                           Set total cloud-drive occupancy cap in MB. -1 keeps
                           the disk-backed default of 95% host capacity.
  --cloud-drive-max-size SIZE
                           Same cap with units, e.g. 1024M, 10G, 1.5TB.
                           A bare number means MB.
  --max-content-mb MB,
  --upload-request-max-mb MB
                           Override HTML_LEARNING_MAX_CONTENT_MB for large
                           upload QA. Blank keeps app/root setting default
                           (8192 MB unless changed in root settings).
  --dry-run                Print resolved config and exit before copying/starting
  --backup [PATH]          Create a runtime-state backup archive and exit.
  --restore PATH           Restore runtime state from a --backup archive and exit.
  --reset                  Reset runtime state, preserving keys and creating a
                           storage/.reset_orphan_recovery bundle before clearing
                           DB/catalog state. See the command catalog.
  --delete                 Delete selected runtime root. Preserves internal storage
                           by moving it aside; external storage roots are not deleted.
  Command catalog:         docs/TEST_FOR_DEVELOP_COMMAND_CATALOG.md
  --run-root PATH          Use a fixed /tmp run root instead of auto-generating one
  --runtime-root PATH,
  --runtime-dir PATH       Use PATH as the runtime directory instead of the
                           layout default. Also configurable with
                           HACKME_DEV_RUNTIME_ROOT. The path may be relative to
                           the current repo; it must not be the repo root.
  --in-place, --no-copy    Launch from the current repo; runtime still uses run-root
  --runtime-in-source,
  --source-runtime,
  --deploy-in-place        Launch from the current repo and write runtime/ there.
                           This is the local deployment layout, not isolated QA.
  --tmp-runtime            With --in-place, keep runtime under --run-root
  --copy                   Force the default /tmp copied source workspace
  --skip-install           Reuse runtime/venv or current Python environment
  --requirements-file PATH  Install this requirements file from the copied runtime.
                           Choices: requirements-minimal.txt, requirements-dev.txt,
                           requirements-games.txt, requirements-comfyui.txt,
                           requirements-hf.txt, requirements.txt.
  --foreground             Run in the foreground instead of nohup background mode
  --root-password VALUE    Default: root
  --manager-password VALUE Default: admin
  --test-password VALUE    Default: test
  -h, --help               Show this help
USAGE
}

say() {
  printf '%s\n' "$*"
}

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

normalize_feature_mode() {
  FEATURE_MODE="${FEATURE_MODE,,}"
  case "$FEATURE_MODE" in
    all|defaults|bundles|custom)
      ;;
    default)
      FEATURE_MODE="defaults"
      ;;
    bundle|package|packages|preset|presets)
      FEATURE_MODE="bundles"
      ;;
    *)
      die "feature mode must be all, defaults, bundles, or custom: $FEATURE_MODE"
      ;;
  esac
}

normalize_yes_no_value() {
  local value="${1,,}"
  case "$value" in
    1|true|yes|y|on|enable|enabled)
      NORMALIZED_YES_NO=1
      ;;
    0|false|no|n|off|disable|disabled)
      NORMALIZED_YES_NO=0
      ;;
    *)
      die "$2 must be on/off, yes/no, true/false, or 1/0: $1"
      ;;
  esac
}

normalize_server_mode() {
  SERVER_MODE="${SERVER_MODE,,}"
  case "$SERVER_MODE" in
    production|preprod|dev_ready|internal_test|test|superweak|maintenance|incident_lockdown)
      ;;
    dev|development)
      SERVER_MODE="dev_ready"
      ;;
    internal)
      SERVER_MODE="internal_test"
      ;;
    *)
      die "server mode must be production, preprod, dev_ready, internal_test, test, superweak, maintenance, or incident_lockdown: $SERVER_MODE"
      ;;
  esac
}

normalize_server_runner() {
  SERVER_RUNNER="${SERVER_RUNNER,,}"
  case "$SERVER_RUNNER" in
    flask|werkzeug)
      SERVER_RUNNER="flask"
      ;;
    gunicorn|wsgi)
      SERVER_RUNNER="gunicorn"
      ;;
    *)
      die "server runner must be flask or gunicorn: $SERVER_RUNNER"
      ;;
  esac
}

normalize_capacity_probe_mode() {
  CAPACITY_PROBE_MODE="${CAPACITY_PROBE_MODE,,}"
  case "$CAPACITY_PROBE_MODE" in
    ""|auto|default)
      CAPACITY_PROBE_MODE="auto"
      ;;
    1|true|yes|y|on|enable|enabled|force|refresh|retest|probe)
      CAPACITY_PROBE_MODE="force"
      ;;
    0|false|no|n|off|disable|disabled|never|skip)
      CAPACITY_PROBE_MODE="never"
      ;;
    *)
      die "capacity probe mode must be auto, force, or never: $CAPACITY_PROBE_MODE"
      ;;
  esac
}

normalize_capacity_probe_tier() {
  CAPACITY_PROBE_TIER="${CAPACITY_PROBE_TIER,,}"
  case "$CAPACITY_PROBE_TIER" in
    ""|auto|default)
      CAPACITY_PROBE_TIER="auto"
      ;;
    sbc|single-board|single-board-computer|board|tiny)
      CAPACITY_PROBE_TIER="sbc"
      ;;
    legacy|old|old-desktop|low-power|nas)
      CAPACITY_PROBE_TIER="legacy"
      ;;
    laptop|notebook)
      CAPACITY_PROBE_TIER="laptop"
      ;;
    midrange|mid-range)
      CAPACITY_PROBE_TIER="midrange"
      ;;
    highend|high-end|top|top-tier|full)
      CAPACITY_PROBE_TIER="highend"
      ;;
    *)
      die "capacity probe tier must be auto, sbc, legacy, laptop, midrange, or highend: $CAPACITY_PROBE_TIER"
      ;;
  esac
}

normalize_cloud_drive_storage_root() {
  if [[ -z "$CLOUD_DRIVE_STORAGE_ROOT" ]]; then
    return 0
  fi
  local normalized
  if ! normalized="$(PYTHONPATH="$SOURCE_ROOT" python3 - "$CLOUD_DRIVE_STORAGE_ROOT" "$SOURCE_ROOT" <<'PY'
import sys

from services.storage.paths import validate_storage_root

raw_root = str(sys.argv[1] or "").strip()
base_dir = str(sys.argv[2] or "").strip()
try:
    print(str(validate_storage_root(raw_root, base_dir=base_dir, create=False)))
except ValueError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(2)
PY
)"; then
    die "cloud drive storage root is unsafe or invalid: $CLOUD_DRIVE_STORAGE_ROOT"
  fi
  CLOUD_DRIVE_STORAGE_ROOT="$normalized"
}

normalize_hls_slot_probe_mode() {
  HLS_SLOT_PROBE_MODE="${HLS_SLOT_PROBE_MODE,,}"
  case "$HLS_SLOT_PROBE_MODE" in
    ask|auto|prompt|force|on|yes|true|1|never|off|no|false|0)
      ;;
    *)
      die "HLS slot probe mode must be ask, force, or never: $HLS_SLOT_PROBE_MODE"
      ;;
  esac
  case "$HLS_SLOT_PROBE_MODE" in
    auto|prompt) HLS_SLOT_PROBE_MODE="ask" ;;
    on|yes|true|1) HLS_SLOT_PROBE_MODE="force" ;;
    off|no|false|0) HLS_SLOT_PROBE_MODE="never" ;;
  esac
}

normalize_transmission_setup_mode() {
  normalize_yes_no_value "$SETUP_TRANSMISSION_BACKEND" "setup transmission backend"
  SETUP_TRANSMISSION_BACKEND="$NORMALIZED_YES_NO"
  if [[ "$SETUP_TRANSMISSION_BACKEND" == "1" ]]; then
    BT_DOWNLOAD_BACKEND="transmission"
  fi
  normalize_yes_no_value "$TRANSMISSION_SETUP_ALLOW_ANY_RPC_IP" "allow any Transmission RPC IP"
  TRANSMISSION_SETUP_ALLOW_ANY_RPC_IP="$NORMALIZED_YES_NO"
}

normalize_cloud_drive_capacity_limit() {
  if [[ -z "$CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB" ]]; then
    return 0
  fi
  local normalized
  if ! normalized="$(python3 - "$CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB" <<'PY'
import math
import re
import sys

raw_value = str(sys.argv[1] or "").strip().lower()
if not raw_value:
    print("")
    raise SystemExit(0)
if raw_value in {"default", "auto"}:
    print("")
    raise SystemExit(0)
if raw_value in {"-1", "none", "unlimited", "disk"}:
    print("-1")
    raise SystemExit(0)

match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgt]?i?b?|mb|gb|tb)?", raw_value)
if not match:
    print("cloud drive max size must be MB, -1, or a size like 1024M/10G/1.5TB", file=sys.stderr)
    raise SystemExit(2)

amount = float(match.group(1))
unit = (match.group(2) or "mb").lower()
unit_multipliers = {
    "": 1024 ** 2,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024 ** 2,
    "mb": 1024 ** 2,
    "mib": 1024 ** 2,
    "g": 1024 ** 3,
    "gb": 1024 ** 3,
    "gib": 1024 ** 3,
    "t": 1024 ** 4,
    "tb": 1024 ** 4,
    "tib": 1024 ** 4,
}
if unit not in unit_multipliers:
    print(f"unknown cloud drive max size unit: {unit}", file=sys.stderr)
    raise SystemExit(2)
limit_mb = int(math.ceil((amount * unit_multipliers[unit]) / (1024 ** 2)))
if limit_mb < 0:
    print("cloud drive max size must be -1 or non-negative", file=sys.stderr)
    raise SystemExit(2)
print(str(limit_mb))
PY
)"; then
    die "invalid cloud drive max size: $CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB"
  fi
  CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB="$normalized"
}

normalize_cloud_drive_options() {
  normalize_cloud_drive_storage_root
  normalize_cloud_drive_capacity_limit
}


normalize_custom_runtime_root() {
  if [[ -z "$CUSTOM_RUNTIME_ROOT" ]]; then
    return 0
  fi
  local normalized
  if ! normalized="$(python3 - "$CUSTOM_RUNTIME_ROOT" "$SOURCE_ROOT" <<'INNERPY'
import sys
from pathlib import Path

raw = str(sys.argv[1] or "").strip()
source = Path(sys.argv[2]).resolve()
if not raw:
    raise SystemExit(0)
path = Path(raw).expanduser()
if not path.is_absolute():
    path = source / path
path = path.resolve(strict=False)
if path == source:
    print("runtime root must not be the repository root", file=sys.stderr)
    raise SystemExit(2)
if path == source.parent:
    print("runtime root must not be the repository parent directory", file=sys.stderr)
    raise SystemExit(2)
print(str(path))
INNERPY
)"; then
    die "runtime root is unsafe or invalid: $CUSTOM_RUNTIME_ROOT"
  fi
  CUSTOM_RUNTIME_ROOT="$normalized"
}

normalize_max_content_option() {
  if [[ -z "$MAX_CONTENT_MB" ]]; then
    return 0
  fi
  [[ "$MAX_CONTENT_MB" =~ ^[0-9]+$ ]] || die "max content MB must be a positive integer"
  (( MAX_CONTENT_MB >= 128 )) || die "max content MB must be at least 128"
}

normalize_session_idle_timeout_option() {
  if [[ -z "$SESSION_IDLE_TIMEOUT_MINUTES" ]]; then
    return 0
  fi
  [[ "$SESSION_IDLE_TIMEOUT_MINUTES" =~ ^[0-9]+$ ]] || die "session idle timeout minutes must be an integer from 0 to 1440"
  (( SESSION_IDLE_TIMEOUT_MINUTES <= 1440 )) || die "session idle timeout minutes must be 0-1440"
}

maybe_run_capacity_probe_for_gunicorn_defaults() {
  if [[ "$SERVER_RUNNER" != "gunicorn" ]]; then
    return 0
  fi

  normalize_capacity_probe_mode
  normalize_capacity_probe_tier
  load_local_capacity_report_defaults || load_local_capacity_defaults

  if [[ "$CAPACITY_PROBE_MODE" == "force" ]]; then
    if [[ "$DRY_RUN" == "1" ]]; then
      say "[dev-tmp] capacity probe: dry-run requested refresh, but dry-run does not run probes"
      return 0
    fi
    run_capacity_probe_for_defaults
    return 0
  fi

  if ! gunicorn_capacity_auto_requested; then
    return 0
  fi

  if [[ "$CAPACITY_PROBE_MODE" == "never" ]]; then
    say "[dev-tmp] capacity probe: disabled; resolving auto with conservative hardware fallback"
    return 0
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    say "[dev-tmp] capacity probe: no local defaults found; dry-run resolves auto with hardware fallback"
    return 0
  fi

  say "[dev-tmp] capacity probe: no local defaults found; auto will run one isolated probe now"
  run_capacity_probe_for_defaults
}

resolve_auto_gunicorn_settings() {
  if [[ "$SERVER_RUNNER" != "gunicorn" ]]; then
    return 0
  fi
  if [[ "${GUNICORN_WORKERS,,}" != "auto" && "${GUNICORN_THREADS,,}" != "auto" ]]; then
    return 0
  fi
  local resolved
  resolved="$(python3 - "$GUNICORN_WORKERS" "$GUNICORN_THREADS" <<'PY'
import os
import sys

raw_workers = str(sys.argv[1] or "auto").strip().lower()
raw_threads = str(sys.argv[2] or "auto").strip().lower()
cpu = max(1, os.cpu_count() or 1)
try:
    mem_mb = int(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024))
except Exception:
    mem_mb = 0

def auto_workers():
    if cpu <= 2 or (mem_mb and mem_mb < 2048):
        return 1
    if cpu >= 16 and (not mem_mb or mem_mb >= 16384):
        return 5
    if cpu >= 8 and (not mem_mb or mem_mb >= 8192):
        return 4
    return 2

def auto_threads():
    if mem_mb and mem_mb < 2048:
        return 4
    if cpu <= 2 or (mem_mb and mem_mb < 4096):
        return 6
    if cpu <= 4:
        return 6
    # This app has substantial SQLite, PointsChain, and governance write
    # serialization. Prefer more worker processes with fewer threads over a
    # single process with a large thread pile; it uses more cores for CPU-bound
    # Python work without multiplying per-process DB writer pressure.
    return 6

workers = auto_workers() if raw_workers in {"", "auto", "dynamic"} else int(raw_workers)
threads = auto_threads() if raw_threads in {"", "auto", "dynamic"} else int(raw_threads)
workers = max(1, min(6, workers))
threads = max(2, min(16, threads))
print(f"{workers} {threads}")
PY
)"
  GUNICORN_WORKERS="${resolved%% *}"
  GUNICORN_THREADS="${resolved##* }"
}

normalize_port_conflict_action() {
  PORT_CONFLICT_ACTION="${PORT_CONFLICT_ACTION,,}"
  if [[ -z "$PORT_CONFLICT_ACTION" ]]; then
    if [[ "$CLI_MODE" == "1" ]]; then
      PORT_CONFLICT_ACTION="fallback"
    else
      PORT_CONFLICT_ACTION="ask"
    fi
  fi
  case "$PORT_CONFLICT_ACTION" in
    ask|kill|fallback|fail)
      ;;
    port)
      PORT_CONFLICT_ACTION="fallback"
      ;;
    quit|error)
      PORT_CONFLICT_ACTION="fail"
      ;;
    *)
      die "port conflict action must be ask, kill, fallback, or fail: $PORT_CONFLICT_ACTION"
      ;;
  esac
}

normalize_runtime_options() {
  normalize_feature_mode
  if [[ "$FEATURE_MODE" == "bundles" ]]; then
    [[ -n "$FEATURE_BUNDLES" ]] || die "feature mode bundles requires --feature-bundles or an interactive bundle selection"
    normalize_feature_or_bundle_selection "$FEATURE_BUNDLES" "bundle" || die "invalid feature bundle selection: $FEATURE_BUNDLES"
    FEATURE_LIST="$NORMALIZED_FEATURE_SELECTION"
  elif [[ "$FEATURE_MODE" == "custom" ]]; then
    if [[ "$FEATURE_LIST_FINALIZED" != "1" ]]; then
      normalize_feature_or_bundle_selection "$FEATURE_LIST" || die "invalid feature selection: $FEATURE_LIST"
      FEATURE_LIST="$NORMALIZED_FEATURE_SELECTION"
    fi
  fi
  normalize_server_mode
  normalize_token_feature_selection "$DEV_TOKEN_FEATURES" || die "invalid generated dev token feature selection: $DEV_TOKEN_FEATURES"
  DEV_TOKEN_FEATURES="$NORMALIZED_DEV_TOKEN_FEATURES"
  normalize_server_runner
  if [[ -n "$REQUIREMENTS_FILE" ]]; then
    normalize_requirements_files "$REQUIREMENTS_FILE" || die "invalid requirements file selection: $REQUIREMENTS_FILE"
    REQUIREMENTS_FILE="$NORMALIZED_REQUIREMENTS_FILES"
  fi
  normalize_cloud_drive_options
  normalize_custom_runtime_root
  normalize_max_content_option
  normalize_session_idle_timeout_option
  normalize_transmission_setup_mode
  maybe_run_capacity_probe_for_gunicorn_defaults
  resolve_auto_gunicorn_settings
  normalize_port_conflict_action
  normalize_yes_no_value "$IN_PLACE" "in-place"
  IN_PLACE="$NORMALIZED_YES_NO"
  normalize_yes_no_value "$RUNTIME_IN_SOURCE" "runtime in source"
  RUNTIME_IN_SOURCE="$NORMALIZED_YES_NO"
  if [[ "$RUNTIME_IN_SOURCE" == "1" ]]; then
    IN_PLACE=1
  fi
  normalize_yes_no_value "$SECURITY_SETTINGS_ENABLED" "security"
  SECURITY_SETTINGS_ENABLED="$NORMALIZED_YES_NO"
  normalize_yes_no_value "$BTC_TRADE_AUTOSTART" "btc trade autostart"
  BTC_TRADE_AUTOSTART="$NORMALIZED_YES_NO"
  normalize_yes_no_value "$BACKTEST_PROBE_ON_STARTUP" "backtest probe on startup"
  BACKTEST_PROBE_ON_STARTUP="$NORMALIZED_YES_NO"
  normalize_yes_no_value "$TRADING_BACKGROUND_DEV_READY" "trading background dev_ready"
  TRADING_BACKGROUND_DEV_READY="$NORMALIZED_YES_NO"
}

append_unique_csv_value() {
  local target_var="$1"
  local raw_value="$2"
  local current_value="${!target_var:-}"
  local candidate existing trimmed
  local _csv_items=()
  candidate="${raw_value#"${raw_value%%[![:space:]]*}"}"
  candidate="${candidate%"${candidate##*[![:space:]]}"}"
  [[ -n "$candidate" ]] || return 0
  IFS=',' read -r -a _csv_items <<< "$current_value"
  for existing in "${_csv_items[@]:-}"; do
    trimmed="${existing#"${existing%%[![:space:]]*}"}"
    trimmed="${trimmed%"${trimmed##*[![:space:]]}"}"
    [[ "$trimmed" == "$candidate" ]] && return 0
  done
  if [[ -z "$current_value" ]]; then
    printf -v "$target_var" '%s' "$candidate"
  else
    printf -v "$target_var" '%s,%s' "$current_value" "$candidate"
  fi
}

normalize_trusted_host_value() {
  local value="$1"
  value="${value#http://}"
  value="${value#https://}"
  value="${value%%/*}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  NORMALIZED_TRUSTED_HOST_VALUE="$value"
}

append_trusted_host_variants() {
  local raw_value="$1"
  local value host_without_port
  normalize_trusted_host_value "$raw_value"
  value="$NORMALIZED_TRUSTED_HOST_VALUE"
  [[ -n "$value" ]] || return 0
  append_unique_csv_value TRUSTED_HOSTS "$value"
  case "$value" in
    \[*\]|*:*:*)
      return 0
      ;;
    *:*)
      host_without_port="${value%%:*}"
      append_unique_csv_value TRUSTED_HOSTS "$host_without_port"
      ;;
    *)
      if [[ -n "$PORT" ]]; then
        append_unique_csv_value TRUSTED_HOSTS "$value:$PORT"
      fi
      ;;
  esac
}

finalize_trusted_hosts() {
  local original_hosts item
  local _trusted_items=()
  original_hosts="$TRUSTED_HOSTS"
  TRUSTED_HOSTS=""
  if [[ -n "$original_hosts" ]]; then
    IFS=',' read -r -a _trusted_items <<< "$original_hosts"
    for item in "${_trusted_items[@]:-}"; do
      append_trusted_host_variants "$item"
    done
  fi
  if [[ -n "$PUBLIC_HOST" ]]; then
    normalize_trusted_host_value "$PUBLIC_HOST"
    PUBLIC_HOST="$NORMALIZED_TRUSTED_HOST_VALUE"
    append_trusted_host_variants "$PUBLIC_HOST"
  fi
}

append_csv_value() {
  local target_var="$1"
  local value="$2"
  local current_value="${!target_var:-}"
  if [[ -z "$current_value" ]]; then
    printf -v "$target_var" '%s' "$value"
  else
    printf -v "$target_var" '%s,%s' "$current_value" "$value"
  fi
}

print_resolved_config() {
  say "[dev-tmp] config:"
  say "  cli:                 $CLI_MODE"
  local default_runtime_root=""
  if [[ "$RUNTIME_IN_SOURCE" == "1" ]]; then
    say "  run_root:            <not used; source runtime>"
    say "  launch_mode:         source runtime deployment"
    default_runtime_root="$SOURCE_ROOT/runtime"
  elif [[ "$IN_PLACE" == "1" ]]; then
    if [[ -n "$CUSTOM_RUNTIME_ROOT" ]]; then
      say "  run_root:            <not used; custom runtime>"
      say "  launch_mode:         in-place (no source copy; custom runtime)"
    else
      say "  run_root:            ${RUN_ROOT:-/tmp/hackme_web_dev_${RUN_ID}_$$}"
      say "  launch_mode:         in-place (no source copy; tmp runtime)"
    fi
    default_runtime_root="${RUN_ROOT:-/tmp/hackme_web_dev_${RUN_ID}_$$}/runtime"
  else
    say "  run_root:            ${RUN_ROOT:-/tmp/hackme_web_dev_${RUN_ID}_$$}"
    say "  launch_mode:         tmp copy"
    default_runtime_root="${RUN_ROOT:-/tmp/hackme_web_dev_${RUN_ID}_$$}/hackme_web/runtime"
  fi
  if [[ -n "$CUSTOM_RUNTIME_ROOT" ]]; then
    say "  runtime_root:        $CUSTOM_RUNTIME_ROOT (custom; default would be $default_runtime_root)"
  else
    say "  runtime_root:        $default_runtime_root"
  fi
  say "  host:                $HOST"
  say "  port:                $PORT"
  if [[ "$DISABLE_TRUSTED_HOSTS" == "1" ]]; then
    say "  trusted_hosts:       disabled (dev only)"
  else
    say "  trusted_hosts:       ${TRUSTED_HOSTS:-<app default local hosts>}"
  fi
  say "  public_host:         ${PUBLIC_HOST:-<none>}"
  say "  feature_mode:        $FEATURE_MODE"
  if [[ "$FEATURE_MODE" == "bundles" ]]; then
    say "  feature_bundles:     ${FEATURE_BUNDLES:-<none>}"
  fi
  say "  features:            ${FEATURE_LIST:-<none>}"
  say "  token_features:      ${DEV_TOKEN_FEATURES:-<unrestricted>}"
  say "  token_ttl_minutes:   $DEV_TOKEN_TTL_MINUTES"
  say "  token_user:          $DEV_TOKEN_USER"
  say "  token_role:          $DEV_TOKEN_ROLE"
  if [[ -n "$DEV_TOKEN_PASSWORD" ]]; then
    say "  token_password:      <configured>"
  else
  say "  token_password:      <keep existing / auto-generate for new user>"
  fi
  say "  security_enabled:    $SECURITY_SETTINGS_ENABLED"
  if [[ "$SECURITY_SETTINGS_ENABLED" == "1" ]]; then
    say "  password_policy:     enforced"
  else
    say "  password_policy:     dev-disabled (default-password change gate off)"
  fi
  say "  idle_logout_minutes: ${SESSION_IDLE_TIMEOUT_MINUTES:-<profile default>}"
  say "  server_mode:         $SERVER_MODE"
  say "  trading_bg_dev:      $TRADING_BACKGROUND_DEV_READY"
  say "  cloud_drive_root:    ${CLOUD_DRIVE_STORAGE_ROOT:-<runtime/storage>}"
  say "  cloud_drive_max_mb:  ${CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB:-<default disk 95%>}"
  say "  max_content_mb:      ${MAX_CONTENT_MB:-<app default>}"
  say "  server_runner:       $SERVER_RUNNER"
  if [[ "$SERVER_RUNNER" == "gunicorn" ]]; then
    say "  gunicorn:            workers=$GUNICORN_WORKERS threads=$GUNICORN_THREADS timeout=$GUNICORN_TIMEOUT backlog=$GUNICORN_BACKLOG max_requests=$GUNICORN_MAX_REQUESTS jitter=$GUNICORN_MAX_REQUESTS_JITTER"
    say "  hls_slots:           max_concurrent=${HACKME_MEDIA_HLS_MAX_CONCURRENT:-<worker default 1>} serialize_all=${HACKME_MEDIA_HLS_SERIALIZE_ALL:-<worker default>} probe=$HLS_SLOT_PROBE_MODE"
    say "  remote_download:     global=${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL:-<root/env default 1>} per_user=${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER:-<root/env default 1>}"
    say "  bt_backend:          ${BT_DOWNLOAD_BACKEND:-${HACKME_BT_BACKEND:-auto}} transmission_rpc=${TRANSMISSION_RPC_URL:-${HACKME_TRANSMISSION_RPC_URL:-http://127.0.0.1:9091/transmission/rpc}}"
    say "  transmission_setup:  ${SETUP_TRANSMISSION_BACKEND:-0} helper=${TRANSMISSION_SETUP_SCRIPT:-scripts/storage/setup_transmission_backend.sh}"
    say "  transmission_daemon: bind=${TRANSMISSION_SETUP_RPC_BIND_ADDRESS:-<helper default>} allow_any_ip=${TRANSMISSION_SETUP_ALLOW_ANY_RPC_IP:-0} whitelist=${TRANSMISSION_SETUP_RPC_WHITELIST:-<helper default>} whitelist_enabled=${TRANSMISSION_SETUP_RPC_WHITELIST_ENABLED:-<helper default>}"
    say "  transmission_auth:   username=$([[ -n "$TRANSMISSION_RPC_USERNAME" ]] && printf configured || printf '<blank>') password=$([[ -n "$TRANSMISSION_RPC_PASSWORD" ]] && printf configured || printf '<blank>')"
    say "  bt_staging_dir:      ${BT_DOWNLOAD_STAGING_DIR:-${HACKME_BT_DOWNLOAD_STAGING_DIR:-<system temp fallback>}}"
    if [[ -n "$HLS_SLOT_PROBE_REPORT_FILE" ]]; then
      say "  hls_slot_report:     $HLS_SLOT_PROBE_REPORT_FILE"
    fi
    say "  capacity_defaults:   $CAPACITY_DEFAULTS_FILE"
    say "  capacity_report:     $CAPACITY_REPORT_DEFAULTS_FILE"
    say "  capacity_probe:      $CAPACITY_PROBE_MODE"
    say "  capacity_tier:       $CAPACITY_PROBE_TIER"
    if [[ "$CAPACITY_PROBE_TIER" == "highend" ]]; then
      say "  capacity_warning:    highend has no account/round ceiling and may freeze or crash the host"
    fi
  fi
  if [[ -n "$EXTRA_ACCOUNTS" ]]; then
    say "  extra_accounts:      <configured>"
  else
    say "  extra_accounts:      <none>"
  fi
  say "  port_conflict:       $PORT_CONFLICT_ACTION"
  say "  skip_install:        $SKIP_INSTALL"
  say "  requirements_files: ${REQUIREMENTS_FILE:-<skip install>}"
  say "  foreground:          $FOREGROUND"
  say "  btc_trade_autostart: $BTC_TRADE_AUTOSTART"
  say "  backtest_probe:      $BACKTEST_PROBE_ON_STARTUP"
}

prompt_value() {
  local label="$1"
  local default_value="$2"
  local target_var="$3"
  local prompt_answer
  printf '%s [%s]: ' "$label" "$default_value"
  if ! read -r prompt_answer; then
    die "interactive setup was interrupted"
  fi
  if [[ -z "$prompt_answer" ]]; then
    prompt_answer="$default_value"
  fi
  printf -v "$target_var" '%s' "$prompt_answer"
}

requirements_file_description() {
  case "${1:-}" in
    requirements-minimal.txt) echo "minimal Flask/runtime startup layer; currently includes games because routes import them" ;;
    requirements-games.txt) echo "game/puzzle dependencies only; redundant when minimal is selected today" ;;
    requirements-dev.txt) echo "developer/browser-test tooling only; combine manually when needed" ;;
    requirements-comfyui.txt) echo "external ComfyUI API integration notes/no-op layer" ;;
    requirements-hf.txt) echo "heavy local Hugging Face/Diffusers backend" ;;
    requirements.txt) echo "aggregate -r bundle: minimal + dev + comfyui; mostly references other files" ;;
    *) echo "custom requirements file" ;;
  esac
}


normalize_requirements_files() {
  local raw="${1:-}"
  raw="${raw//,/ }"
  local token mapped
  local result=()
  local seen=" "
  for token in $raw; do
    case "${token,,}" in
      1|default|compat|requirements.txt) mapped="requirements.txt" ;;
      2|minimal|requirements-minimal.txt) mapped="requirements-minimal.txt" ;;
      3|dev|requirements-dev.txt) mapped="requirements-dev.txt" ;;
      4|comfyui|requirements-comfyui.txt) mapped="requirements-comfyui.txt" ;;
      5|hf|huggingface|requirements-hf.txt) mapped="requirements-hf.txt" ;;
      6|games|game|requirements-games.txt) mapped="requirements-games.txt" ;;
      requirements*.txt) mapped="$token" ;;
      *) return 1 ;;
    esac
    if [[ "$seen" != *" $mapped "* ]]; then
      result+=("$mapped")
      seen+="$mapped "
    fi
  done
  [[ ${#result[@]} -gt 0 ]] || return 1
  if [[ "$seen" == *" requirements-minimal.txt "* && "$seen" == *" requirements-games.txt "* ]]; then
    local compact=()
    for mapped in "${result[@]}"; do
      [[ "$mapped" == "requirements-games.txt" ]] && continue
      compact+=("$mapped")
    done
    result=("${compact[@]}")
  fi
  NORMALIZED_REQUIREMENTS_FILES="${result[*]}"
}

requirements_files_description() {
  local file
  local output=""
  for file in $REQUIREMENTS_FILE; do
    if [[ -n "$output" ]]; then
      output+="; "
    fi
    output+="$file - $(requirements_file_description "$file")"
  done
  echo "$output"
}


feature_selection_text_for_requirements() {
  printf '%s %s %s' "$FEATURE_MODE" "$FEATURE_BUNDLES" "$FEATURE_LIST"
}

recommend_requirements_for_feature_selection() {
  local selected=" $(feature_selection_text_for_requirements) "
  local recommended="requirements-minimal.txt"
  if [[ "$FEATURE_MODE" == "all" || "$selected" == *"feature_comfyui_enabled"* || "$selected" == *" ai"* || "$selected" == *" full-user"* || "$selected" == *" qa-all"* ]]; then
    recommended+=" requirements-comfyui.txt"
  fi
  if [[ "$selected" == *"feature_hf"* || "$selected" == *"huggingface"* || "$selected" == *"diffusers"* || "$selected" == *"local-ai"* || "$selected" == *"local_ai"* ]]; then
    recommended+=" requirements-hf.txt"
  fi
  if [[ "$SERVER_MODE" == "test" || "$SERVER_MODE" == "internal_test" || "$selected" == *" qa"* || "$selected" == *"requirements-dev"* ]]; then
    recommended+=" requirements-dev.txt"
  fi
  normalize_requirements_files "$recommended" || return 1
  RECOMMENDED_REQUIREMENTS_FILES="$NORMALIZED_REQUIREMENTS_FILES"
}

prompt_requirements_from_features() {
  local answer
  local recommended_desc
  if [[ "$SKIP_INSTALL" == "1" ]]; then
    print_requirements_feature_guidance
    return 0
  fi
  if [[ "$REQUIREMENTS_FILE_SET" == "1" ]]; then
    print_requirements_feature_guidance
    return 0
  fi
  recommend_requirements_for_feature_selection || return 0
  recommended_desc="$RECOMMENDED_REQUIREMENTS_FILES"
  if [[ "$RECOMMENDED_REQUIREMENTS_FILES" == "$REQUIREMENTS_FILE" ]]; then
    say "Recommended dependency files already selected: $RECOMMENDED_REQUIREMENTS_FILES"
    print_requirements_feature_guidance
    return 0
  fi
  say "Recommended dependency files from selected features: $recommended_desc"
  local saved_requirements="$REQUIREMENTS_FILE"
  REQUIREMENTS_FILE="$RECOMMENDED_REQUIREMENTS_FILES"
  say "  $(requirements_files_description)"
  REQUIREMENTS_FILE="$saved_requirements"
  while true; do
    printf 'Use recommended dependency files? [Y/n/custom]: '
    if ! read -r answer; then
      die "interactive setup was interrupted"
    fi
    case "${answer,,}" in
      ""|y|yes)
        REQUIREMENTS_FILE="$RECOMMENDED_REQUIREMENTS_FILES"
        print_requirements_feature_guidance
        return 0
        ;;
      n|no|custom|c)
        prompt_requirements_file
        print_requirements_feature_guidance
        return 0
        ;;
      *)
        if normalize_requirements_files "$answer"; then
          REQUIREMENTS_FILE="$NORMALIZED_REQUIREMENTS_FILES"
          print_requirements_feature_guidance
          return 0
        fi
        say "Answer y, n/custom, or enter requirements choices such as 2 3."
        ;;
    esac
  done
}

prompt_requirements_file() {
  local answer
  say "Dependency requirements file(s):"
  say "  1) requirements.txt         $(requirements_file_description requirements.txt)"
  say "  2) requirements-minimal.txt $(requirements_file_description requirements-minimal.txt)"
  say "  3) requirements-dev.txt     $(requirements_file_description requirements-dev.txt)"
  say "  4) requirements-comfyui.txt $(requirements_file_description requirements-comfyui.txt)"
  say "  5) requirements-hf.txt      $(requirements_file_description requirements-hf.txt)"
  say "  6) requirements-games.txt   $(requirements_file_description requirements-games.txt)"
  say "     Multiple choices are allowed, e.g. 2 3 or 2,6. Note: 2 already includes 6 today."
  while true; do
    printf 'Choose requirements file(s) [%s]: ' "$REQUIREMENTS_FILE"
    if ! read -r answer; then
      die "interactive setup was interrupted"
    fi
    answer="${answer:-$REQUIREMENTS_FILE}"
    if normalize_requirements_files "$answer"; then
      REQUIREMENTS_FILE="$NORMALIZED_REQUIREMENTS_FILES"
      return 0
    fi
    say "Please choose one or more values from 1-6, aliases, or requirements*.txt files. Example: 2 6"
  done
}

print_requirements_feature_guidance() {
  if [[ "$SKIP_INSTALL" == "1" ]]; then
    say "Feature selection: --skip-install leaves dependencies unchanged, so all feature bundles are shown."
    return 0
  fi
  say "Feature selection dependency baseline: $(requirements_files_description)"
  case " $REQUIREMENTS_FILE " in
    *" requirements-minimal.txt "*) say "  Heavy optional features such as local HF/Diffusers should stay disabled unless installed separately." ;;
  esac
  case " $REQUIREMENTS_FILE " in
    *" requirements-games.txt "*) say "  Games dependencies are selected; minimal currently also includes this layer for startup compatibility." ;;
  esac
  case " $REQUIREMENTS_FILE " in
    *" requirements-hf.txt "*) say "  HF/Diffusers features are available; external ComfyUI still uses its own server endpoint." ;;
  esac
  case " $REQUIREMENTS_FILE " in
    *" requirements-dev.txt "*) say "  Dev tooling is selected; enable feature bundles only if runtime deps are also present." ;;
  esac
}

prompt_yes_no() {
  local label="$1"
  local default_value="$2"
  local target_var="$3"
  local answer
  local suffix
  if [[ "$default_value" == "1" ]]; then
    suffix="Y/n"
  else
    suffix="y/N"
  fi
  while true; do
    printf '%s [%s]: ' "$label" "$suffix"
    if ! read -r answer; then
      die "interactive setup was interrupted"
    fi
    case "${answer,,}" in
      "")
        printf -v "$target_var" '%s' "$default_value"
        return 0
        ;;
      y|yes)
        printf -v "$target_var" '%s' "1"
        return 0
        ;;
      n|no)
        printf -v "$target_var" '%s' "0"
        return 0
        ;;
      *)
        say "Please answer y or n."
        ;;
    esac
  done
}

load_capacity_probe_report_summary() {
  local report_path="$1"
  local summary
  [[ -n "$report_path" && -s "$report_path" ]] || return 1
  if ! summary="$($PYTHON_BIN - "$report_path" <<'REPORTPY'
import json
import shlex
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    report = json.loads(path.read_text(encoding="utf-8"))
except Exception as exc:
    print("CAPACITY_REPORT_OK=0")
    print(f"CAPACITY_REPORT_ERROR={shlex.quote(type(exc).__name__ + ': ' + str(exc))}")
    raise SystemExit(0)

recommendation = report.get("recommendation") or {}
limits = report.get("limits") or {}
load = report.get("load") or {}
thresholds = report.get("thresholds") or {}
profiles = report.get("profiles") or []
rc1_gate = report.get("rc1_capacity_gate") or {}

workers = int(recommendation.get("workers") or 0) if recommendation.get("ok") else 0
threads = int(recommendation.get("threads") or 0) if recommendation.get("ok") else 0
accounts = int(recommendation.get("max_passing_accounts") or 0) if recommendation.get("ok") else 0
selected_round = None
selected_probe = {}
for profile_result in profiles:
    profile = profile_result.get("profile") or {}
    if int(profile.get("workers") or 0) != workers or int(profile.get("threads") or 0) != threads:
        continue
    for round_result in profile_result.get("rounds") or []:
        if int(round_result.get("accounts") or 0) == accounts:
            selected_round = round_result
            selected_probe = round_result.get("probe") or {}
            break
    if selected_round:
        break

latency = selected_probe.get("latency_ms") or recommendation.get("observed_latency_ms") or {}
status_counts = selected_probe.get("status_counts") or recommendation.get("observed_status_counts") or {}
cpu = selected_probe.get("cpu") or recommendation.get("observed_cpu") or {}
by_label = selected_probe.get("by_label_latency_ms") or {}
slowest = sorted(by_label.items(), key=lambda item: int((item[1] or {}).get("p95") or 0), reverse=True)[:8]
labels = sorted(by_label)
profile_parts = []
profile_errors = []
for profile_result in profiles:
    profile = profile_result.get("profile") or {}
    label = profile.get("label") or f"{profile.get('workers')}x{profile.get('threads')}"
    round_accounts = [str(int((round_item or {}).get("accounts") or 0)) for round_item in profile_result.get("rounds") or []]
    if round_accounts:
        profile_parts.append(f"{label}: accounts {'/'.join(round_accounts)}")
    else:
        err = str(profile_result.get("error") or "").strip()
        if err:
            compact_err = " ".join(err.split())[:240]
            profile_errors.append(f"{label}: {compact_err}")
        profile_parts.append(f"{label}: no completed rounds")

kind_descriptions = {
    "light": "login plus read-only me/jobs/shares/trading dashboard checks",
    "basic": "login, read-only me/jobs/shares/trading/games checks, one small text upload, and drive preview",
    "normal": "login, points wallet/ledger/transfer/governance/disputes, trading dashboard/spot/bots/grid/margin, chat/community, cloud-drive upload/preview/share/albums, appeals, game score",
    "malicious": "SQL/XSS-style chat/community probes, invalid game score, invalid trading/governance/dispute payloads, forbidden drive access, bad CSRF",
    "heavy": "repeated drive preview/download/update, resumable upload chunks, trading backtests/export, smart album organize",
}
load_kinds = [str(item) for item in (load.get("kinds") or [])]
load_description = " | ".join(f"{kind}: {kind_descriptions.get(kind, kind)}" for kind in load_kinds)
experience = limits.get("experience") or {}
application_limit = limits.get("application_limit") or {}
server_instability = limits.get("server_instability") or {}
ux_start = experience.get("degradation_starts_at") or {}
max_before_ux = experience.get("max_accounts_before_degradation") or {}
app_start = application_limit.get("first_observed_at") or {}
server_start = server_instability.get("first_observed_at") or {}

def scalar(value, default=""):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)

def emit(key, value):
    print(f"{key}={shlex.quote(scalar(value))}")

emit("CAPACITY_REPORT_OK", "1" if recommendation.get("ok") else "0")
emit("CAPACITY_REPORT_ERROR", recommendation.get("msg") or "")
emit("CAPACITY_REPORT_PATH", str(path))
emit("CAPACITY_REPORT_WORKERS", workers or "")
emit("CAPACITY_REPORT_THREADS", threads or "")
suggested_env = recommendation.get("suggested_env") or {}
emit("CAPACITY_REPORT_MAX_REQUESTS", suggested_env.get("HACKME_DEV_GUNICORN_MAX_REQUESTS") or "")
emit("CAPACITY_REPORT_MAX_REQUESTS_JITTER", suggested_env.get("HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER") or "")
emit("CAPACITY_REPORT_PROFILE", f"{workers}x{threads}" if workers and threads else "")
emit("CAPACITY_REPORT_TOTAL_LANES", workers * threads if workers and threads else "")
emit("CAPACITY_REPORT_BACKPRESSURE", suggested_env.get("HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY") or (max(4, threads) if threads else ""))
emit("CAPACITY_REPORT_HLS_MAX_CONCURRENT", suggested_env.get("HACKME_MEDIA_HLS_MAX_CONCURRENT") or "")
emit("CAPACITY_REPORT_HLS_SERIALIZE_ALL", suggested_env.get("HACKME_MEDIA_HLS_SERIALIZE_ALL") or "")
emit("CAPACITY_REPORT_HLS_POLICY", json.dumps(recommendation.get("hls_capacity_policy") or {}, sort_keys=True, separators=(",", ":")))
emit("CAPACITY_REPORT_REMOTE_DOWNLOAD_GLOBAL", suggested_env.get("HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL") or "")
emit("CAPACITY_REPORT_REMOTE_DOWNLOAD_PER_USER", suggested_env.get("HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER") or "")
emit("CAPACITY_REPORT_REMOTE_DOWNLOAD_POLICY", json.dumps(recommendation.get("remote_download_capacity_policy") or {}, sort_keys=True, separators=(",", ":")))
emit("CAPACITY_REPORT_MAX_SAFE_ACCOUNTS", accounts or "")
emit("CAPACITY_REPORT_TARGET_P95_MS", recommendation.get("target_p95_ms") or thresholds.get("target_p95_ms") or "")
emit("CAPACITY_REPORT_MAX_DURATION_SECONDS", thresholds.get("max_duration_seconds") or "")
emit("CAPACITY_REPORT_LAT_P50", latency.get("p50") or "")
emit("CAPACITY_REPORT_LAT_P95", latency.get("p95") or "")
emit("CAPACITY_REPORT_LAT_P99", latency.get("p99") or "")
emit("CAPACITY_REPORT_LAT_MAX", latency.get("max") or "")
emit("CAPACITY_REPORT_STATUS_COUNTS", json.dumps(status_counts, sort_keys=True, separators=(",", ":")))
emit("CAPACITY_REPORT_HARD_FAILURES", selected_probe.get("hard_failure_count") if selected_probe else "")
emit("CAPACITY_REPORT_APP_LIMITS", selected_probe.get("app_limit_count") if selected_probe else "")
emit("CAPACITY_REPORT_SERVER_FAILURES", selected_probe.get("server_failure_count") if selected_probe else "")
emit("CAPACITY_REPORT_CPU_ACTIVE_WORKERS", cpu.get("active_worker_peak") or "")
emit("CAPACITY_REPORT_CPU_PEAK", cpu.get("total_worker_cpu_peak_percent") or "")
emit("CAPACITY_REPORT_TIER", load.get("capacity_tier") or "")
emit("CAPACITY_REPORT_TIER_DESCRIPTION", load.get("capacity_tier_description") or "")
emit("CAPACITY_REPORT_LOAD_PROFILE", load.get("profile") or "")
emit("CAPACITY_REPORT_LOAD_KINDS", ",".join(load_kinds))
emit("CAPACITY_REPORT_LOAD_DESCRIPTION", load_description)
emit("CAPACITY_REPORT_HEAVY_REPEAT", load.get("heavy_repeat") or "")
emit("CAPACITY_REPORT_HEAVY_UPLOAD_BYTES", load.get("heavy_upload_bytes") or "")
emit("CAPACITY_REPORT_TESTED_PROFILES", " ; ".join(profile_parts))
emit("CAPACITY_REPORT_PROFILE_ERRORS", " | ".join(profile_errors))
emit("CAPACITY_REPORT_TESTED_LABEL_COUNT", len(labels))
emit("CAPACITY_REPORT_TESTED_LABELS", ", ".join(labels[:24]) + (" ..." if len(labels) > 24 else ""))
emit("CAPACITY_REPORT_SLOWEST_LABELS", " | ".join(f"{label}:p95={(stats or {}).get('p95', '-')}ms p99={(stats or {}).get('p99', '-')}ms max={(stats or {}).get('max', '-')}ms" for label, stats in slowest))
emit("CAPACITY_REPORT_MAX_BEFORE_UX", max_before_ux.get("accounts") or "")
emit("CAPACITY_REPORT_UX_DEGRADATION", ux_start.get("accounts") or "not_reached")
emit("CAPACITY_REPORT_APP_LIMIT_AT", app_start.get("accounts") or "not_reached")
emit("CAPACITY_REPORT_SERVER_INSTABILITY", server_start.get("accounts") or server_instability.get("status") or "not_reached")
emit("CAPACITY_REPORT_RC1_GATE", "PASS" if rc1_gate.get("pass") else "FAIL")
emit("CAPACITY_REPORT_RC1_REASONS", ",".join(str(item) for item in (rc1_gate.get("reasons") or [])))
REPORTPY
  )"; then
    return 1
  fi
  eval "$summary"
  if [[ "${CAPACITY_REPORT_OK:-0}" == "1" ]]; then
    GUNICORN_WORKERS="$CAPACITY_REPORT_WORKERS"
    GUNICORN_THREADS="$CAPACITY_REPORT_THREADS"
    export HACKME_DEV_GUNICORN_WORKERS="$GUNICORN_WORKERS"
    export HACKME_DEV_GUNICORN_THREADS="$GUNICORN_THREADS"
    if [[ -n "${CAPACITY_REPORT_MAX_REQUESTS:-}" ]]; then
      GUNICORN_MAX_REQUESTS="$CAPACITY_REPORT_MAX_REQUESTS"
      export HACKME_DEV_GUNICORN_MAX_REQUESTS="$GUNICORN_MAX_REQUESTS"
    fi
    if [[ -n "${CAPACITY_REPORT_MAX_REQUESTS_JITTER:-}" ]]; then
      GUNICORN_MAX_REQUESTS_JITTER="$CAPACITY_REPORT_MAX_REQUESTS_JITTER"
      export HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER="$GUNICORN_MAX_REQUESTS_JITTER"
    fi
    if [[ -n "${CAPACITY_REPORT_BACKPRESSURE:-}" ]]; then
      export HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY="$CAPACITY_REPORT_BACKPRESSURE"
    fi
    if [[ -n "${CAPACITY_REPORT_HLS_MAX_CONCURRENT:-}" ]]; then
      export HACKME_MEDIA_HLS_MAX_CONCURRENT="$CAPACITY_REPORT_HLS_MAX_CONCURRENT"
    fi
    if [[ -n "${CAPACITY_REPORT_HLS_SERIALIZE_ALL:-}" ]]; then
      export HACKME_MEDIA_HLS_SERIALIZE_ALL="$CAPACITY_REPORT_HLS_SERIALIZE_ALL"
    fi
    if [[ -n "${CAPACITY_REPORT_REMOTE_DOWNLOAD_GLOBAL:-}" ]]; then
      export HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL="$CAPACITY_REPORT_REMOTE_DOWNLOAD_GLOBAL"
    fi
    if [[ -n "${CAPACITY_REPORT_REMOTE_DOWNLOAD_PER_USER:-}" ]]; then
      export HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER="$CAPACITY_REPORT_REMOTE_DOWNLOAD_PER_USER"
    fi
  fi
  return 0
}

print_capacity_probe_conclusion() {
  local backpressure_capacity="${HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY:-auto}"
  local hls_max_concurrent="${HACKME_MEDIA_HLS_MAX_CONCURRENT:-auto}"
  local hls_serialize_all="${HACKME_MEDIA_HLS_SERIALIZE_ALL:-auto}"
  local remote_download_global="${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL:-auto}"
  local remote_download_per_user="${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER:-auto}"
  say "[dev-tmp] capacity probe conclusion:"
  if [[ "${CAPACITY_REPORT_OK:-}" == "1" ]]; then
    say "  recommendation:                 ${CAPACITY_REPORT_PROFILE} (${CAPACITY_REPORT_TOTAL_LANES} worker-thread lanes)"
    if [[ -n "${CAPACITY_REPORT_TIER:-}" ]]; then
      say "  hardware_tier:                  ${CAPACITY_REPORT_TIER}${CAPACITY_REPORT_TIER_DESCRIPTION:+ - $CAPACITY_REPORT_TIER_DESCRIPTION}"
    fi
    if [[ -n "${CAPACITY_REPORT_MAX_DURATION_SECONDS:-}" && "${CAPACITY_REPORT_MAX_DURATION_SECONDS:-0}" != "0" ]]; then
      say "  time_limit:                     ${CAPACITY_REPORT_MAX_DURATION_SECONDS}s"
    fi
    say "  max_safe_accounts:              ${CAPACITY_REPORT_MAX_SAFE_ACCOUNTS} concurrent accounts under target p95<=${CAPACITY_REPORT_TARGET_P95_MS}ms"
    say "  selected_round_latency:         p50=${CAPACITY_REPORT_LAT_P50}ms p95=${CAPACITY_REPORT_LAT_P95}ms p99=${CAPACITY_REPORT_LAT_P99}ms max=${CAPACITY_REPORT_LAT_MAX}ms"
    say "  selected_round_statuses:        ${CAPACITY_REPORT_STATUS_COUNTS}"
    say "  selected_round_failures:        hard=${CAPACITY_REPORT_HARD_FAILURES:-0} server=${CAPACITY_REPORT_SERVER_FAILURES:-0} app_limit=${CAPACITY_REPORT_APP_LIMITS:-0}"
    say "  selected_round_cpu:             active_workers=${CAPACITY_REPORT_CPU_ACTIVE_WORKERS:-?} worker_cpu_peak=${CAPACITY_REPORT_CPU_PEAK:-?}%"
    if [[ -n "${CAPACITY_REPORT_HLS_POLICY:-}" ]]; then
      say "  hls_capacity_policy:            ${CAPACITY_REPORT_HLS_POLICY}"
    fi
    if [[ -n "${CAPACITY_REPORT_REMOTE_DOWNLOAD_POLICY:-}" ]]; then
      say "  remote_download_policy:         ${CAPACITY_REPORT_REMOTE_DOWNLOAD_POLICY}"
    fi
    say "  tested_profiles:                ${CAPACITY_REPORT_TESTED_PROFILES}"
    say "  tested_load:                    ${CAPACITY_REPORT_LOAD_PROFILE} (${CAPACITY_REPORT_LOAD_KINDS})"
    say "  tested_operations:              ${CAPACITY_REPORT_LOAD_DESCRIPTION}"
    say "  selected_round_labels:          ${CAPACITY_REPORT_TESTED_LABEL_COUNT} labels; ${CAPACITY_REPORT_TESTED_LABELS}"
    if [[ -n "${CAPACITY_REPORT_SLOWEST_LABELS:-}" ]]; then
      say "  slowest_labels:                 ${CAPACITY_REPORT_SLOWEST_LABELS}"
    fi
    if [[ "${CAPACITY_REPORT_UX_DEGRADATION}" == "not_reached" ]]; then
      say "  ux_degradation_at:              not_reached (max before UX degradation: ${CAPACITY_REPORT_MAX_BEFORE_UX:-unknown})"
    else
      say "  ux_degradation_at:              ${CAPACITY_REPORT_UX_DEGRADATION} accounts (max before UX degradation: ${CAPACITY_REPORT_MAX_BEFORE_UX:-unknown})"
    fi
    say "  application_limit_at:           ${CAPACITY_REPORT_APP_LIMIT_AT}"
    say "  server_instability_at:          ${CAPACITY_REPORT_SERVER_INSTABILITY}"
    say "  rc1_capacity_gate:              ${CAPACITY_REPORT_RC1_GATE}${CAPACITY_REPORT_RC1_REASONS:+ reasons=$CAPACITY_REPORT_RC1_REASONS}"
    say "  report:                         ${CAPACITY_REPORT_PATH}"
  else
    say "  recommendation:                 unavailable${CAPACITY_REPORT_ERROR:+ ($CAPACITY_REPORT_ERROR)}"
    if [[ -n "${CAPACITY_REPORT_TESTED_PROFILES:-}" ]]; then
      say "  tested_profiles:                ${CAPACITY_REPORT_TESTED_PROFILES}"
    fi
    if [[ -n "${CAPACITY_REPORT_PROFILE_ERRORS:-}" ]]; then
      say "  profile_errors:                 ${CAPACITY_REPORT_PROFILE_ERRORS}"
    fi
    if [[ -n "${CAPACITY_REPORT_LOAD_PROFILE:-}" ]]; then
      say "  tested_load:                    ${CAPACITY_REPORT_LOAD_PROFILE} (${CAPACITY_REPORT_LOAD_KINDS})"
    fi
    if [[ -n "${CAPACITY_REPORT_TIER:-}" ]]; then
      say "  hardware_tier:                  ${CAPACITY_REPORT_TIER}${CAPACITY_REPORT_TIER_DESCRIPTION:+ - $CAPACITY_REPORT_TIER_DESCRIPTION}"
    fi
    if [[ -n "${CAPACITY_REPORT_MAX_DURATION_SECONDS:-}" && "${CAPACITY_REPORT_MAX_DURATION_SECONDS:-0}" != "0" ]]; then
      say "  time_limit:                     ${CAPACITY_REPORT_MAX_DURATION_SECONDS}s"
    fi
    if [[ -n "${CAPACITY_REPORT_PATH:-}" ]]; then
      say "  report:                         ${CAPACITY_REPORT_PATH}"
    fi
  fi
  say "  gunicorn_workers:               $GUNICORN_WORKERS"
  say "  gunicorn_threads_per_worker:    $GUNICORN_THREADS"
  say "  backpressure_thread_capacity:   $backpressure_capacity"
  say "  hls_max_concurrent:             $hls_max_concurrent"
  say "  hls_serialize_all:              $hls_serialize_all"
  say "  remote_download_global:         $remote_download_global"
  say "  remote_download_per_user:       $remote_download_per_user"
  say "  gunicorn_max_requests:          $GUNICORN_MAX_REQUESTS"
  say "  gunicorn_max_requests_jitter:   $GUNICORN_MAX_REQUESTS_JITTER"
}

run_hls_slot_probe_for_startup() {
  [[ "$HLS_SLOT_PROBE_RAN" == "1" ]] && return 0
  HLS_SLOT_PROBE_RAN=1
  local report runtime_root suggested serialize_all
  runtime_root="${TMPDIR:-/tmp}/hackme_hls_slot_probe_${RUN_ID}_$$"
  report="${TMPDIR:-/tmp}/hackme_hls_slot_probe_${RUN_ID}_$$.json"
  HLS_SLOT_PROBE_REPORT_FILE="$report"
  say "[dev-tmp] HLS slot probe: starting quick Premium HLS sizing check"
  say "[dev-tmp] HLS slot probe: report file $report"
  if "$PYTHON_BIN" "$SOURCE_ROOT/scripts/testing/hls_premium_sizing_probe.py"       --runtime-root "$runtime_root"       --json-out "$report"       --jobs 2       --max-concurrent 2       --duration 8       --fixture-size 640x360       --fixture-rate 24       --video-bitrate 700k       --hls-profile mobile_saver       --hls-original-variant-mode never       --worker-timeout 180 >/dev/null; then
    suggested="$($PYTHON_BIN - "$report" <<'HLSPROBEJSON'
import json
import sys
from pathlib import Path
try:
    report = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    rec = report.get("sizing_recommendation") or {}
    print(int(rec.get("suggested_hls_max_concurrent") or 1))
except Exception:
    print(1)
HLSPROBEJSON
)"
    serialize_all="1"
    [[ "$suggested" =~ ^[0-9]+$ ]] || suggested="1"
    if (( suggested < 1 )); then suggested="1"; fi
    if (( suggested > 4 )); then suggested="4"; fi
    local quality_cap final_suggested
    quality_cap="${HACKME_MEDIA_HLS_MAX_CONCURRENT:-1}"
    [[ "$quality_cap" =~ ^[0-9]+$ ]] || quality_cap="1"
    if (( quality_cap < 1 )); then quality_cap="1"; fi
    final_suggested="$suggested"
    if (( final_suggested > quality_cap )); then
      final_suggested="$quality_cap"
    fi
    export HACKME_MEDIA_HLS_MAX_CONCURRENT="$final_suggested"
    export HACKME_MEDIA_HLS_SERIALIZE_ALL="$serialize_all"
    say "[dev-tmp] HLS slot probe: raw_suggested=$suggested quality_cap=$quality_cap final_max_concurrent=$final_suggested serialize_all=$serialize_all"
    say "[dev-tmp] HLS slot probe: applied quality-capped result to this launch"
    return 0
  fi
  say "[dev-tmp] HLS slot probe: failed; keeping existing HLS slot setting (${HACKME_MEDIA_HLS_MAX_CONCURRENT:-worker default 1})"
  return 1
}

prompt_hls_slot_probe() {
  [[ "$SERVER_RUNNER" == "gunicorn" ]] || return 0
  normalize_hls_slot_probe_mode
  if [[ "$HLS_SLOT_PROBE_MODE" == "never" ]]; then
    return 0
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    say "[dev-tmp] HLS slot probe: dry-run, not running HLS sizing test"
    return 0
  fi
  if [[ "$HLS_SLOT_PROBE_MODE" == "force" ]]; then
    run_hls_slot_probe_for_startup || true
    return 0
  fi
  [[ "$CLI_MODE" == "1" ]] && return 0
  say "Startup test items:"
  say "  HLS slot sizing checks whether Premium HLS can safely use more than one ffmpeg worker."
  say "  It uses a short mobile_saver fixture; skip it for the fastest startup."
  local answer=0
  prompt_yes_no "Run quick HLS slot sizing before launch" 0 answer
  if [[ "$answer" == "1" ]]; then
    run_hls_slot_probe_for_startup || true
  fi
}

normalize_bt_download_backend() {
  BT_DOWNLOAD_BACKEND="${BT_DOWNLOAD_BACKEND:-auto}"
  case "${BT_DOWNLOAD_BACKEND,,}" in
    auto|transmission|aria2) BT_DOWNLOAD_BACKEND="${BT_DOWNLOAD_BACKEND,,}" ;;
    aria2c) BT_DOWNLOAD_BACKEND="aria2" ;;
    transmission-rpc|rpc) BT_DOWNLOAD_BACKEND="transmission" ;;
    *) die "BT backend must be auto, transmission, or aria2" ;;
  esac
}

prompt_bt_backend_choice() {
  local answer
  normalize_bt_download_backend
  say "BT/magnet remote-download backend:"
  say "  1) auto - prefer Transmission RPC, fallback aria2 when RPC is unavailable"
  say "  2) transmission - require Transmission RPC"
  say "  3) aria2 - force aria2c"
  while true; do
    printf 'Choose BT backend [%s]: ' "$BT_DOWNLOAD_BACKEND"
    if ! read -r answer; then
      die "interactive setup was interrupted"
    fi
    answer="${answer:-$BT_DOWNLOAD_BACKEND}"
    case "${answer,,}" in
      1|auto) BT_DOWNLOAD_BACKEND="auto"; return 0 ;;
      2|transmission|transmission-rpc|rpc) BT_DOWNLOAD_BACKEND="transmission"; return 0 ;;
      3|aria2|aria2c) BT_DOWNLOAD_BACKEND="aria2"; return 0 ;;
      *) say "Please choose 1, 2, 3, auto, transmission, or aria2." ;;
    esac
  done
}

prompt_transmission_link_mode() {
  local answer
  say "Transmission backend setup:"
  say "  1) automatic - install/configure transmission-daemon now via sudo/root helper"
  say "  2) manual    - I already configured Transmission; enter RPC URL, account, and staging/download dir"
  say "  3) skip      - leave Transmission setup disabled for now"
  while true; do
    printf 'Choose Transmission setup mode [1]: '
    if ! read -r answer; then
      die "interactive setup was interrupted"
    fi
    answer="${answer:-1}"
    case "${answer,,}" in
      1|auto|automatic|install|setup)
        SETUP_TRANSMISSION_BACKEND=1
        BT_DOWNLOAD_BACKEND="transmission"
        say "Transmission automatic setup will install/configure transmission-daemon via sudo/root when launch starts."
        say "The helper will generate or apply RPC credentials and report the staging/download directory back to this script."
        export HACKME_DEV_SETUP_TRANSMISSION_BACKEND="$SETUP_TRANSMISSION_BACKEND"
        return 0
        ;;
      2|manual|custom|self|existing)
        SETUP_TRANSMISSION_BACKEND=0
        BT_DOWNLOAD_BACKEND="transmission"
        prompt_value "Transmission RPC URL" "$TRANSMISSION_RPC_URL" TRANSMISSION_RPC_URL
        prompt_value "Transmission RPC username (blank = no auth)" "$TRANSMISSION_RPC_USERNAME" TRANSMISSION_RPC_USERNAME
        prompt_value "Transmission RPC password (blank = no auth)" "$TRANSMISSION_RPC_PASSWORD" TRANSMISSION_RPC_PASSWORD
        while [[ -z "$BT_DOWNLOAD_STAGING_DIR" ]]; do
          prompt_value "Transmission staging/download directory for hackme_web to read completed files" "$BT_DOWNLOAD_STAGING_DIR" BT_DOWNLOAD_STAGING_DIR
          [[ -n "$BT_DOWNLOAD_STAGING_DIR" ]] || say "Please provide the directory that hackme_web should link/import from."
        done
        export HACKME_BT_DOWNLOAD_STAGING_DIR="$BT_DOWNLOAD_STAGING_DIR"
        return 0
        ;;
      3|skip|none|no)
        SETUP_TRANSMISSION_BACKEND=0
        return 0
        ;;
      *)
        say "Please choose 1, 2, or 3."
        ;;
    esac
  done
}

prompt_remote_download_settings() {
  local default_global="${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL:-1}"
  local default_per_user="${HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER:-1}"

  normalize_bt_download_backend
  if [[ "$BT_DOWNLOAD_CONFIG_SET" == "1" || "$TRANSMISSION_CONFIG_SET" == "1" || "$REMOTE_DOWNLOAD_LIMITS_SET" == "1" ]]; then
    say "[dev-tmp] remote download: using explicit env/CLI settings; skipping interactive backend prompts"
  else
    say "Remote download / BT settings can also be changed later from root system settings."
    prompt_bt_backend_choice
    if [[ "$BT_DOWNLOAD_BACKEND" == "transmission" ]]; then
      prompt_transmission_link_mode
    fi
  fi

  if [[ "$REMOTE_DOWNLOAD_LIMITS_SET" != "1" ]]; then
    prompt_capacity_integer "Remote download global concurrency" "$default_global" HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL
    prompt_capacity_integer "Remote download per-user concurrency" "$default_per_user" HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER
  fi

  export HACKME_BT_BACKEND="$BT_DOWNLOAD_BACKEND"
  export HACKME_TRANSMISSION_RPC_URL="$TRANSMISSION_RPC_URL"
  export HACKME_TRANSMISSION_RPC_USERNAME="$TRANSMISSION_RPC_USERNAME"
  export HACKME_TRANSMISSION_RPC_PASSWORD="$TRANSMISSION_RPC_PASSWORD"
  export HACKME_BT_DOWNLOAD_STAGING_DIR="$BT_DOWNLOAD_STAGING_DIR"
  export HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL
  export HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER
}

transmission_rpc_port_from_url() {
  "$PYTHON_BIN" - "$TRANSMISSION_RPC_URL" <<'PY'
import sys
from urllib.parse import urlparse

raw = str(sys.argv[1] or "").strip()
parsed = urlparse(raw)
if parsed.scheme not in {"http", "https"}:
    print("Transmission RPC URL must be http(s)", file=sys.stderr)
    raise SystemExit(2)
if not parsed.hostname:
    print("Transmission RPC URL must include a host", file=sys.stderr)
    raise SystemExit(2)
print(parsed.port or (443 if parsed.scheme == "https" else 80))
PY
}

transmission_rpc_web_url() {
  local rpc_url="${TRANSMISSION_RPC_URL:-}"
  case "$rpc_url" in
    */rpc/)
      printf '%s/web/\n' "${rpc_url%/rpc/}"
      ;;
    */rpc)
      printf '%s/web/\n' "${rpc_url%/rpc}"
      ;;
    *)
      printf '%s\n' "$rpc_url"
      ;;
  esac
}

print_transmission_access_summary() {
  local backend="${BT_DOWNLOAD_BACKEND:-${HACKME_BT_BACKEND:-auto}}"
  if [[ "$backend" != "transmission" && "$SETUP_TRANSMISSION_BACKEND" != "1" && "$TRANSMISSION_CONFIG_SET" != "1" ]]; then
    return 0
  fi
  say "[dev-tmp] transmission_rpc:      ${TRANSMISSION_RPC_URL:-<blank>}"
  say "[dev-tmp] transmission_web:      $(transmission_rpc_web_url)"
  say "[dev-tmp] transmission_user:     ${TRANSMISSION_RPC_USERNAME:-<blank>}"
  say "[dev-tmp] transmission_password: ${TRANSMISSION_RPC_PASSWORD:-<blank>}"
}

run_transmission_backend_setup_if_requested() {
  [[ "$SETUP_TRANSMISSION_BACKEND" == "1" ]] || return 0
  [[ -f "$TRANSMISSION_SETUP_SCRIPT" ]] || die "Transmission setup helper not found: $TRANSMISSION_SETUP_SCRIPT"
  [[ -n "${RUNTIME_ROOT:-}" && -n "${EFFECTIVE_STORAGE_ROOT:-}" ]] || die "runtime/storage root must be resolved before Transmission setup"

  local rpc_port
  if ! rpc_port="$(transmission_rpc_port_from_url)"; then
    die "invalid Transmission RPC URL: $TRANSMISSION_RPC_URL"
  fi

  local setup_log="$RUNTIME_ROOT/logs/transmission_setup.out"
  local app_user="${SUDO_USER:-$(id -un)}"
  local cmd=()
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    cmd=(bash "$TRANSMISSION_SETUP_SCRIPT")
  else
    command -v sudo >/dev/null 2>&1 || die "sudo is required for --setup-transmission-backend"
    cmd=(sudo bash "$TRANSMISSION_SETUP_SCRIPT")
  fi
  local helper_args=(
    --storage-root "$EFFECTIVE_STORAGE_ROOT"
    --settings-file "$TRANSMISSION_SETUP_SETTINGS_FILE"
    --service "$TRANSMISSION_SETUP_SERVICE"
    --app-user "$app_user"
    --rpc-port "$rpc_port"
  )
  if [[ -n "$TRANSMISSION_RPC_USERNAME" ]]; then
    helper_args+=(--rpc-username "$TRANSMISSION_RPC_USERNAME")
  fi
  if [[ -n "$TRANSMISSION_RPC_PASSWORD" ]]; then
    helper_args+=(--rpc-password "$TRANSMISSION_RPC_PASSWORD")
  fi
  if [[ -n "$TRANSMISSION_SETUP_RPC_BIND_ADDRESS" ]]; then
    helper_args+=(--rpc-bind-address "$TRANSMISSION_SETUP_RPC_BIND_ADDRESS")
  fi
  if [[ -n "$TRANSMISSION_SETUP_RPC_WHITELIST" ]]; then
    helper_args+=(--rpc-whitelist "$TRANSMISSION_SETUP_RPC_WHITELIST")
  fi
  if [[ -n "$TRANSMISSION_SETUP_RPC_WHITELIST_ENABLED" ]]; then
    helper_args+=(--rpc-whitelist-enabled "$TRANSMISSION_SETUP_RPC_WHITELIST_ENABLED")
  fi
  if [[ -n "$TRANSMISSION_SETUP_RPC_AUTHENTICATION_REQUIRED" ]]; then
    helper_args+=(--rpc-authentication-required "$TRANSMISSION_SETUP_RPC_AUTHENTICATION_REQUIRED")
  fi
  if [[ "$TRANSMISSION_SETUP_ALLOW_ANY_RPC_IP" == "1" ]]; then
    helper_args+=(--allow-any-rpc-ip)
  fi

  say "[dev-tmp] transmission: configuring daemon via existing helper"
  say "[dev-tmp] transmission: log $setup_log"
  touch "$setup_log"
  chmod 600 "$setup_log" || true
  if ! "${cmd[@]}" "${helper_args[@]}" >"$setup_log" 2>&1; then
    tail -n 80 "$setup_log" >&2 || true
    die "Transmission setup helper failed; see $setup_log"
  fi
  chmod 600 "$setup_log" || true

  local helper_url helper_username helper_password helper_staging
  helper_url="$(sed -n 's/^[[:space:]]*Transmission RPC URL:[[:space:]]*//p' "$setup_log" | tail -n 1)"
  helper_username="$(sed -n 's/^[[:space:]]*Transmission RPC username:[[:space:]]*//p' "$setup_log" | tail -n 1)"
  helper_password="$(sed -n 's/^[[:space:]]*Transmission RPC password:[[:space:]]*//p' "$setup_log" | tail -n 1)"
  helper_staging="$(sed -n 's/^[[:space:]]*HACKME_BT_DOWNLOAD_STAGING_DIR=//p' "$setup_log" | tail -n 1)"
  [[ -n "$helper_url" ]] && TRANSMISSION_RPC_URL="$helper_url"
  [[ -n "$helper_username" ]] && TRANSMISSION_RPC_USERNAME="$helper_username"
  [[ -n "$helper_password" ]] && TRANSMISSION_RPC_PASSWORD="$helper_password"
  [[ -n "$helper_staging" ]] && BT_DOWNLOAD_STAGING_DIR="$helper_staging"
  [[ -n "$BT_DOWNLOAD_STAGING_DIR" ]] || die "Transmission setup helper did not report HACKME_BT_DOWNLOAD_STAGING_DIR"

  export HACKME_BT_BACKEND="$BT_DOWNLOAD_BACKEND"
  export HACKME_TRANSMISSION_RPC_URL="$TRANSMISSION_RPC_URL"
  export HACKME_TRANSMISSION_RPC_USERNAME="$TRANSMISSION_RPC_USERNAME"
  export HACKME_TRANSMISSION_RPC_PASSWORD="$TRANSMISSION_RPC_PASSWORD"
  export HACKME_BT_DOWNLOAD_STAGING_DIR="$BT_DOWNLOAD_STAGING_DIR"
  say "[dev-tmp] transmission: configured RPC $TRANSMISSION_RPC_URL, staging $BT_DOWNLOAD_STAGING_DIR"
  print_transmission_access_summary
}

prompt_capacity_integer() {
  local label="$1"
  local default_value="$2"
  local target_var="$3"
  local allow_zero="${4:-0}"
  local answer
  while true; do
    prompt_value "$label" "$default_value" answer
    answer="${answer:-}"
    if [[ "$answer" =~ ^[0-9]+$ ]] && { [[ "$allow_zero" == "1" ]] || (( answer > 0 )); }; then
      printf -v "$target_var" '%s' "$answer"
      return 0
    fi
    if [[ "$allow_zero" == "1" ]]; then
      say "Please enter 0 or a positive integer."
    else
      say "Please enter a positive integer."
    fi
  done
}

prompt_manual_capacity_settings() {
  prompt_capacity_integer "Manual Gunicorn workers" "$GUNICORN_WORKERS" GUNICORN_WORKERS
  prompt_capacity_integer "Manual Gunicorn threads per worker" "$GUNICORN_THREADS" GUNICORN_THREADS
  prompt_capacity_integer "Manual backpressure thread capacity" "${HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY:-$GUNICORN_THREADS}" HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY
  prompt_capacity_integer "Manual Gunicorn max requests" "$GUNICORN_MAX_REQUESTS" GUNICORN_MAX_REQUESTS 1
  prompt_capacity_integer "Manual Gunicorn max requests jitter" "$GUNICORN_MAX_REQUESTS_JITTER" GUNICORN_MAX_REQUESTS_JITTER 1
  export HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY
  export HACKME_DEV_GUNICORN_WORKERS="$GUNICORN_WORKERS"
  export HACKME_DEV_GUNICORN_THREADS="$GUNICORN_THREADS"
  export HACKME_DEV_GUNICORN_MAX_REQUESTS="$GUNICORN_MAX_REQUESTS"
  export HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER="$GUNICORN_MAX_REQUESTS_JITTER"
  CAPACITY_PROBE_MODE="never"
  CAPACITY_SETTINGS_FINALIZED=1
  say "[dev-tmp] capacity probe: using manual capacity/backpressure parameters"
  print_capacity_probe_conclusion
}

reset_capacity_to_conservative_fallback() {
  GUNICORN_WORKERS="auto"
  GUNICORN_THREADS="auto"
  GUNICORN_MAX_REQUESTS="10000"
  GUNICORN_MAX_REQUESTS_JITTER="1000"
  unset HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY
  unset HACKME_DEV_GUNICORN_WORKERS
  unset HACKME_DEV_GUNICORN_THREADS
  unset HACKME_DEV_GUNICORN_MAX_REQUESTS
  unset HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER
  CAPACITY_PROBE_MODE="never"
  CAPACITY_SETTINGS_FINALIZED=1
  say "[dev-tmp] capacity probe: using conservative hardware fallback; auto settings will be resolved without another probe"
}

prompt_capacity_probe_tier() {
  local answer
  say "Capacity probe hardware tier:"
  say "  1) sbc       Single-board computer / tiny VM; smallest smoke-size probe, 60s cap"
  say "  2) legacy    Old desktop or low-power NAS; low-impact read-only probe, 120s cap"
  say "  3) laptop    Ordinary laptop; small bounded member workflow probe, 180s cap"
  say "  4) midrange  Mid-range host; bounded normal capacity search"
  say "  5) highend   Top-tier host; no account/round ceiling, increases load until a stop target"
  say "  6) auto      Legacy automatic behavior"
  while true; do
    printf 'Choose capacity tier [2]: '
    if ! read -r answer; then
      die "interactive setup was interrupted"
    fi
    answer="${answer:-2}"
    case "${answer,,}" in
      1|sbc|single-board|single-board-computer|board|tiny)
        CAPACITY_PROBE_TIER="sbc"
        return 0
        ;;
      2|legacy|old|old-desktop|low-power|nas)
        CAPACITY_PROBE_TIER="legacy"
        return 0
        ;;
      3|laptop|notebook)
        CAPACITY_PROBE_TIER="laptop"
        return 0
        ;;
      4|midrange|mid-range|server|host)
        CAPACITY_PROBE_TIER="midrange"
        return 0
        ;;
      5|highend|high-end|top|top-tier|full)
        say "WARNING: highend capacity probe has no account/round ceiling. It can freeze or crash the host while searching for the limit."
        prompt_yes_no "Run the dangerous highend capacity probe anyway" 0 answer
        [[ "$answer" == "1" ]] || continue
        CAPACITY_PROBE_TIER="highend"
        return 0
        ;;
      6|auto|default|legacy-auto)
        CAPACITY_PROBE_TIER="auto"
        return 0
        ;;
      *)
        say "Please choose 1, 2, 3, 4, 5, or 6."
        ;;
    esac
  done
}

confirm_capacity_probe_result() {
  local choice
  print_capacity_probe_conclusion
  if [[ "${CAPACITY_REPORT_OK:-0}" != "1" ]]; then
    if [[ "$CLI_MODE" == "1" ]]; then
      die "capacity probe produced no usable recommendation; rerun with install support or pass manual Gunicorn/backpressure settings"
    fi
    say "Capacity/backpressure action:"
    say "  1) fallback Use conservative hardware fallback without another probe"
    say "  2) manual   Enter Gunicorn and backpressure parameters manually"
    say "  3) retest   Run the isolated capacity probe again"
    while true; do
      printf 'Choose capacity action [1]: '
      if ! read -r choice; then
        die "interactive setup was interrupted"
      fi
      choice="${choice:-1}"
      case "${choice,,}" in
        1|fallback|conservative|skip)
          reset_capacity_to_conservative_fallback
          return 0
          ;;
        2|manual|custom)
          prompt_manual_capacity_settings
          return 0
          ;;
        3|retest|retry|rerun)
          say "[dev-tmp] capacity probe: rerunning by user request"
          return 1
          ;;
        *)
          say "Please choose 1, 2, or 3."
          ;;
      esac
    done
  fi

  if [[ "$CLI_MODE" == "1" ]]; then
    say "[dev-tmp] capacity probe: CLI mode applies these defaults automatically"
    return 0
  fi

  say "Capacity/backpressure action:"
  say "  1) apply    Use this probe result for Gunicorn and backpressure"
  say "  2) retest   Run the isolated capacity probe again"
  say "  3) manual   Enter Gunicorn and backpressure parameters manually"
  say "  4) fallback Use conservative hardware fallback without another probe"
  while true; do
    printf 'Choose capacity action [1]: '
    if ! read -r choice; then
      die "interactive setup was interrupted"
    fi
    choice="${choice:-1}"
    case "${choice,,}" in
      1|apply|use|yes|y)
        say "[dev-tmp] capacity probe: applying probe result"
        CAPACITY_SETTINGS_FINALIZED=1
        return 0
        ;;
      2|retest|retry|rerun)
        say "[dev-tmp] capacity probe: rerunning by user request"
        return 1
        ;;
      3|manual|custom)
        prompt_manual_capacity_settings
        return 0
        ;;
      4|fallback|conservative|skip)
        reset_capacity_to_conservative_fallback
        return 0
        ;;
      *)
        say "Please choose 1, 2, 3, or 4."
        ;;
    esac
  done
}

run_capacity_probe_for_defaults() {
  local continue_after_failure=1
  local capacity_report
  if [[ "$CAPACITY_PROBE_RAN" == "1" ]]; then
    say "[dev-tmp] capacity probe: already ran for this launch; reusing loaded defaults"
    return 0
  fi
  while true; do
    CAPACITY_PROBE_RAN=1
    capacity_report="${TMPDIR:-/tmp}/hackme_capacity_probe_report_${RUN_ID}_$$.json"
    CAPACITY_PROBE_REPORT_FILE="$capacity_report"
    say "[dev-tmp] capacity probe: starting isolated pre-deploy probe"
    say "[dev-tmp] capacity probe: defaults file $CAPACITY_DEFAULTS_FILE"
    say "[dev-tmp] capacity probe: report file $capacity_report"
    local probe_install_args=()
    local probe_tier_args=()
    if [[ "${HACKME_DEV_CAPACITY_PROBE_INSTALL:-1}" == "1" ]]; then
      probe_install_args+=(--install)
    fi
    if [[ -n "$CAPACITY_PROBE_TIER" && "$CAPACITY_PROBE_TIER" != "auto" ]]; then
      probe_tier_args+=(--capacity-tier "$CAPACITY_PROBE_TIER")
      say "[dev-tmp] capacity probe: hardware tier $CAPACITY_PROBE_TIER"
      if [[ "$CAPACITY_PROBE_TIER" == "highend" ]]; then
        say "[dev-tmp] WARNING: highend capacity probe has no account/round ceiling and may freeze or crash this host."
      fi
    fi
    if "$PYTHON_BIN" "$SOURCE_ROOT/scripts/testing/predeploy_capacity_probe.py" \
        --capacity-defaults-file "$CAPACITY_DEFAULTS_FILE" \
        --output "$capacity_report" \
        "${probe_tier_args[@]}" \
        "${probe_install_args[@]}"; then
      load_local_capacity_defaults force
      load_capacity_probe_report_summary "$capacity_report" || true
      if [[ -n "$CAPACITY_REPORT_DEFAULTS_FILE" ]]; then
        mkdir -p "$(dirname "$CAPACITY_REPORT_DEFAULTS_FILE")"
        cp "$capacity_report" "$CAPACITY_REPORT_DEFAULTS_FILE"
        say "[dev-tmp] capacity probe: saved report defaults $CAPACITY_REPORT_DEFAULTS_FILE"
      fi
      say "[dev-tmp] capacity probe: loaded workers=$GUNICORN_WORKERS threads=$GUNICORN_THREADS max_requests=$GUNICORN_MAX_REQUESTS jitter=$GUNICORN_MAX_REQUESTS_JITTER"
      if confirm_capacity_probe_result; then
        return 0
      fi
      continue
    fi
    say "[dev-tmp] capacity probe failed."
    if [[ "$CLI_MODE" == "1" ]]; then
      die "capacity probe failed"
    fi
    prompt_yes_no "Continue startup without new capacity defaults" 1 continue_after_failure
    [[ "$continue_after_failure" == "1" ]] || die "capacity probe failed"
    CAPACITY_PROBE_MODE="never"
    return 0
  done
}

prompt_feature_settings() {
  local choice
  local default_choice

  normalize_feature_mode
  case "$FEATURE_MODE" in
    all)
      default_choice="1"
      ;;
    defaults)
      default_choice="2"
      ;;
    bundles)
      default_choice="3"
      ;;
    custom)
      default_choice="4"
      ;;
  esac

  say "Feature mode:"
  say "  1) all-except Enable every service, then choose services/packages to disable"
  say "  2) defaults   Keep server feature defaults"
  say "  3) bundles    Enable only selected feature packages"
  say "  4) custom     Advanced allow-list: enter package names and/or feature_* keys"
  say "  5) all        Enable every server DEFAULT_SETTINGS feature_* flag with no exclusions"
  while true; do
    printf 'Feature mode [%s]: ' "$default_choice"
    if ! read -r choice; then
      die "interactive setup was interrupted"
    fi
    choice="${choice:-$default_choice}"
    case "${choice,,}" in
      1|all-except|except|subtract|subtractive|minus)
        prompt_feature_exclusion_scope
        return 0
        ;;
      2|default|defaults)
        FEATURE_MODE="defaults"
        FEATURE_LIST=""
        FEATURE_BUNDLES=""
        FEATURE_LIST_FINALIZED=0
        return 0
        ;;
      3|bundle|bundles|package|packages|preset|presets)
        FEATURE_MODE="bundles"
        FEATURE_LIST_FINALIZED=0
        prompt_feature_bundle_scope
        return 0
        ;;
      4|custom)
        FEATURE_MODE="custom"
        FEATURE_LIST_FINALIZED=0
        print_known_feature_bundles
        print_known_feature_keys
        prompt_value "Enabled feature packages / keys, comma-separated" "$FEATURE_LIST" FEATURE_LIST
        return 0
        ;;
      5|all)
        FEATURE_MODE="all"
        FEATURE_LIST=""
        FEATURE_BUNDLES=""
        FEATURE_LIST_FINALIZED=0
        return 0
        ;;
      *)
        say "Please choose 1, 2, 3, 4, or 5."
        ;;
    esac
  done
}

print_known_feature_bundles() {
  PYTHONPATH="$SOURCE_ROOT" python3 - <<'PY' 2>/dev/null || true
try:
    from services.platform.settings import FEATURE_FLAG_KEYS
except Exception:
    raise SystemExit(0)

feature_keys = set(FEATURE_FLAG_KEYS)
bundles = [
    ("ops-minimum", "維運骨架 / 帳號 / 健康 / audit", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled",
    )),
    ("minimum-ops", "最低維運 / ops-minimum alias", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled",
    )),
    ("core-admin", "舊名：核心管理 / 健康 / audit", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled",
    )),
    ("safe-community", "安全社群 / 聊天 / 討論 / 申覆 / 檢舉", (
        "feature_accounts_enabled", "feature_chat_enabled", "feature_community_enabled",
        "feature_attachments_enabled", "feature_reports_enabled", "feature_reports_notifications_enabled",
        "feature_appeals_enabled", "feature_violation_center_enabled", "feature_account_security_enabled",
        "feature_social_search_enabled",
    )),
    ("social", "舊名：聊天、討論區、附件、檢舉通知", (
        "feature_chat_enabled", "feature_community_enabled", "feature_attachments_enabled",
        "feature_reports_enabled", "feature_reports_notifications_enabled", "feature_social_search_enabled",
    )),
    ("storage", "雲端硬碟 / E2EE / 相簿", (
        "feature_privacy_uploads_enabled", "feature_storage_albums_enabled", "feature_attachments_enabled",
    )),
    ("creator-media", "創作者影音 / 上傳保存 / 打賞經濟", (
        "feature_accounts_enabled", "feature_videos_enabled", "feature_privacy_uploads_enabled",
        "feature_storage_albums_enabled", "feature_attachments_enabled", "feature_reports_enabled",
        "feature_reports_notifications_enabled", "feature_economy_enabled", "feature_points_chain_enabled",
    )),
    ("media", "舊名：影音分享 / 上傳保存 / 打賞經濟", (
        "feature_videos_enabled", "feature_privacy_uploads_enabled", "feature_economy_enabled",
        "feature_points_chain_enabled",
    )),
    ("games", "遊戲區 / 西洋棋", ("feature_games_enabled",)),
    ("experiments", "實驗區", ("feature_experiments_enabled",)),
    ("ai", "ComfyUI AI 產圖 + 儲存分享", (
        "feature_comfyui_enabled", "feature_privacy_uploads_enabled",
    )),
    ("economy", "基本積分 + PointsChain", ("feature_economy_enabled", "feature_points_chain_enabled")),
    ("points-chain-rc1", "PointsChain RC1 營運組合", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled", "feature_economy_enabled", "feature_points_chain_enabled",
        "feature_violation_center_enabled", "feature_appeals_enabled", "feature_reports_enabled",
        "feature_identity_governance_enabled", "feature_member_governance_enabled",
        "feature_account_security_enabled", "feature_advanced_security_enabled",
    )),
    ("exchange-ops", "交易所營運 / PointsChain + trading", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled", "feature_economy_enabled", "feature_points_chain_enabled",
        "feature_trading_enabled", "feature_violation_center_enabled", "feature_appeals_enabled",
        "feature_reports_enabled", "feature_identity_governance_enabled", "feature_member_governance_enabled",
        "feature_account_security_enabled", "feature_advanced_security_enabled",
    )),
    ("trading", "積分交易所 + PointsChain", (
        "feature_trading_enabled", "feature_economy_enabled", "feature_points_chain_enabled",
    )),
    ("moderation", "申訴、檢舉、違規治理", (
        "feature_accounts_enabled", "feature_appeals_enabled", "feature_reports_enabled",
        "feature_violation_center_enabled", "feature_reports_notifications_enabled",
        "feature_member_governance_enabled", "feature_identity_governance_enabled",
    )),
    ("personalization", "個人外觀與介面客製化", (
        "feature_personalization_enabled", "feature_ui_rebuild_enabled",
    )),
    ("low-resource", "低資源完整前台 / 關閉重型與私有鏈", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled", "feature_chat_enabled", "feature_community_enabled",
        "feature_appeals_enabled", "feature_violation_center_enabled", "feature_reports_enabled",
        "feature_attachments_enabled", "feature_privacy_uploads_enabled", "feature_storage_albums_enabled",
        "feature_economy_enabled", "feature_games_enabled", "feature_social_search_enabled",
        "feature_account_security_enabled", "feature_advanced_security_enabled",
    )),
    ("raspberry-lite", "Raspberry / low-resource alias", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled", "feature_chat_enabled", "feature_community_enabled",
        "feature_appeals_enabled", "feature_violation_center_enabled", "feature_reports_enabled",
        "feature_attachments_enabled", "feature_privacy_uploads_enabled", "feature_storage_albums_enabled",
        "feature_economy_enabled", "feature_games_enabled", "feature_social_search_enabled",
        "feature_account_security_enabled", "feature_advanced_security_enabled",
    )),
    ("full-user", "一般使用者完整體驗", (
        "feature_chat_enabled", "feature_community_enabled", "feature_attachments_enabled",
        "feature_reports_enabled", "feature_reports_notifications_enabled", "feature_appeals_enabled",
        "feature_violation_center_enabled", "feature_privacy_uploads_enabled", "feature_storage_albums_enabled",
        "feature_videos_enabled", "feature_games_enabled", "feature_comfyui_enabled",
        "feature_economy_enabled", "feature_points_chain_enabled", "feature_trading_enabled",
        "feature_personalization_enabled", "feature_social_search_enabled", "feature_account_security_enabled",
    )),
    ("qa-all", "QA / 找碴測試：所有 feature flags", tuple(FEATURE_FLAG_KEYS)),
]

print("Available feature packages:")
for index, (name, label, keys) in enumerate(bundles, 1):
    count = len([key for key in keys if key in feature_keys])
    print(f"  b{index:<2d}) {name}: {label} ({count} feature flags)")
PY
}

print_known_feature_keys() {
  PYTHONPATH="$SOURCE_ROOT" python3 - <<'PY' 2>/dev/null || true
try:
    from services.platform.settings import FEATURE_FLAG_KEYS
    from services.platform.settings_metadata import setting_detail
except Exception:
    raise SystemExit(0)

print("Available individual feature keys:")
for index, key in enumerate(FEATURE_FLAG_KEYS, 1):
    detail = setting_detail(key)
    label = str(detail.get("label") or key).strip()
    print(f"  f{index:<2d}) {key}: {label}")
PY
}

normalize_feature_or_bundle_selection() {
  local raw_value="$1"
  local number_mode="${2:-feature}"
  local include_dependencies="${3:-1}"
  local normalized
  if ! normalized="$(PYTHONPATH="$SOURCE_ROOT" python3 - "$raw_value" "$number_mode" "$include_dependencies" <<'PY'
import re
import sys

raw_value = str(sys.argv[1] or "").strip()
number_mode = str(sys.argv[2] or "feature").strip().lower()
include_dependencies = str(sys.argv[3] or "1").strip().lower() not in {"0", "false", "no", "off"}
try:
    from services.platform.settings import FEATURE_DEPENDENCY_RULES, FEATURE_FLAG_KEYS, normalize_feature_key
except Exception as exc:
    print(f"feature catalog unavailable: {exc}", file=sys.stderr)
    raise SystemExit(2)

feature_keys = list(FEATURE_FLAG_KEYS)
feature_key_set = set(feature_keys)
bundles = [
    ("ops-minimum", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled",
    )),
    ("minimum-ops", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled",
    )),
    ("core-admin", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled",
    )),
    ("safe-community", (
        "feature_accounts_enabled", "feature_chat_enabled", "feature_community_enabled",
        "feature_attachments_enabled", "feature_reports_enabled", "feature_reports_notifications_enabled",
        "feature_appeals_enabled", "feature_violation_center_enabled", "feature_account_security_enabled",
        "feature_social_search_enabled",
    )),
    ("social", (
        "feature_chat_enabled", "feature_community_enabled", "feature_attachments_enabled",
        "feature_reports_enabled", "feature_reports_notifications_enabled", "feature_social_search_enabled",
    )),
    ("storage", (
        "feature_privacy_uploads_enabled", "feature_storage_albums_enabled", "feature_attachments_enabled",
    )),
    ("creator-media", (
        "feature_accounts_enabled", "feature_videos_enabled", "feature_privacy_uploads_enabled",
        "feature_storage_albums_enabled", "feature_attachments_enabled", "feature_reports_enabled",
        "feature_reports_notifications_enabled", "feature_economy_enabled", "feature_points_chain_enabled",
    )),
    ("media", (
        "feature_videos_enabled", "feature_privacy_uploads_enabled", "feature_economy_enabled",
        "feature_points_chain_enabled",
    )),
    ("games", ("feature_games_enabled",)),
    ("experiments", ("feature_experiments_enabled",)),
    ("ai", ("feature_comfyui_enabled", "feature_privacy_uploads_enabled")),
    ("economy", ("feature_economy_enabled", "feature_points_chain_enabled")),
    ("points-chain-rc1", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled", "feature_economy_enabled", "feature_points_chain_enabled",
        "feature_violation_center_enabled", "feature_appeals_enabled", "feature_reports_enabled",
        "feature_identity_governance_enabled", "feature_member_governance_enabled",
        "feature_account_security_enabled", "feature_advanced_security_enabled",
    )),
    ("exchange-ops", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled", "feature_economy_enabled", "feature_points_chain_enabled",
        "feature_trading_enabled", "feature_violation_center_enabled", "feature_appeals_enabled",
        "feature_reports_enabled", "feature_identity_governance_enabled", "feature_member_governance_enabled",
        "feature_account_security_enabled", "feature_advanced_security_enabled",
    )),
    ("trading", ("feature_trading_enabled", "feature_economy_enabled", "feature_points_chain_enabled")),
    ("moderation", (
        "feature_accounts_enabled", "feature_appeals_enabled", "feature_reports_enabled",
        "feature_violation_center_enabled", "feature_reports_notifications_enabled",
        "feature_member_governance_enabled", "feature_identity_governance_enabled",
    )),
    ("personalization", ("feature_personalization_enabled", "feature_ui_rebuild_enabled")),
    ("low-resource", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled", "feature_chat_enabled", "feature_community_enabled",
        "feature_appeals_enabled", "feature_violation_center_enabled", "feature_reports_enabled",
        "feature_attachments_enabled", "feature_privacy_uploads_enabled", "feature_storage_albums_enabled",
        "feature_economy_enabled", "feature_games_enabled", "feature_social_search_enabled",
        "feature_account_security_enabled", "feature_advanced_security_enabled",
    )),
    ("raspberry-lite", (
        "feature_accounts_enabled", "feature_audit_log_enabled", "feature_system_health_enabled",
        "feature_server_modes_enabled", "feature_snapshot_restore_enabled", "feature_health_center_enabled",
        "feature_reports_notifications_enabled", "feature_chat_enabled", "feature_community_enabled",
        "feature_appeals_enabled", "feature_violation_center_enabled", "feature_reports_enabled",
        "feature_attachments_enabled", "feature_privacy_uploads_enabled", "feature_storage_albums_enabled",
        "feature_economy_enabled", "feature_games_enabled", "feature_social_search_enabled",
        "feature_account_security_enabled", "feature_advanced_security_enabled",
    )),
    ("full-user", (
        "feature_chat_enabled", "feature_community_enabled", "feature_attachments_enabled",
        "feature_reports_enabled", "feature_reports_notifications_enabled", "feature_appeals_enabled",
        "feature_violation_center_enabled", "feature_privacy_uploads_enabled", "feature_storage_albums_enabled",
        "feature_videos_enabled", "feature_games_enabled", "feature_comfyui_enabled",
        "feature_economy_enabled", "feature_points_chain_enabled", "feature_trading_enabled",
        "feature_personalization_enabled", "feature_social_search_enabled", "feature_account_security_enabled",
    )),
    ("qa-all", tuple(feature_keys)),
]
bundle_map = {name: tuple(key for key in keys if key in feature_key_set) for name, keys in bundles}
if not raw_value or raw_value.lower() in {"all", "*", "unrestricted", "none", "0"}:
    print("")
    raise SystemExit(0)

allowed = []
unknown = []
for item in re.split(r"[\s,，]+", raw_value):
    choice = item.strip()
    if not choice:
        continue
    lowered = choice.lower()
    if lowered in bundle_map:
        for key in bundle_map[lowered]:
            if key not in allowed:
                allowed.append(key)
        continue
    if lowered.startswith(("bundle:", "package:", "preset:")):
        bundle_name = lowered.split(":", 1)[1]
        if bundle_name in bundle_map:
            for key in bundle_map[bundle_name]:
                if key not in allowed:
                    allowed.append(key)
            continue
        unknown.append(choice)
        continue
    if lowered.startswith(("b", "p")) and lowered[1:].isdigit():
        index = int(lowered[1:])
        if 1 <= index <= len(bundles):
            for key in bundles[index - 1][1]:
                if key in feature_key_set and key not in allowed:
                    allowed.append(key)
            continue
        unknown.append(choice)
        continue
    if lowered.startswith("f") and lowered[1:].isdigit():
        index = int(lowered[1:])
        if 1 <= index <= len(feature_keys):
            key = feature_keys[index - 1]
        else:
            unknown.append(choice)
            continue
        if key not in allowed:
            allowed.append(key)
        continue
    if choice.isdigit():
        index = int(choice)
        if number_mode == "bundle" and 1 <= index <= len(bundles):
            for key in bundles[index - 1][1]:
                if key in feature_key_set and key not in allowed:
                    allowed.append(key)
            continue
        if 1 <= index <= len(feature_keys):
            key = feature_keys[index - 1]
        else:
            unknown.append(choice)
            continue
    else:
        key = normalize_feature_key(choice)
    if key not in feature_key_set:
        unknown.append(choice)
        continue
    if key not in allowed:
        allowed.append(key)

if unknown:
    print(f"unknown feature choice(s): {', '.join(unknown)}", file=sys.stderr)
    raise SystemExit(2)

if include_dependencies:
    changed = True
    while changed:
        changed = False
        for key in list(allowed):
            rule = FEATURE_DEPENDENCY_RULES.get(key, {}) or {}
            for dep in tuple(rule.get("required", ()) or ()) + tuple(rule.get("recommended", ()) or ()):
                dep = normalize_feature_key(dep)
                if dep in feature_key_set and dep not in allowed:
                    allowed.append(dep)
                    changed = True

print(",".join(allowed))
PY
)"; then
    return 1
  fi
  NORMALIZED_FEATURE_SELECTION="$normalized"
  return 0
}

normalize_token_feature_selection() {
  normalize_feature_or_bundle_selection "$1" "feature" || return 1
  NORMALIZED_DEV_TOKEN_FEATURES="$NORMALIZED_FEATURE_SELECTION"
  return 0
}

normalize_feature_exclusion_selection() {
  local raw_value="$1"
  local excluded_csv
  local normalized

  normalize_feature_or_bundle_selection "$raw_value" "feature" 0 || return 1
  excluded_csv="$NORMALIZED_FEATURE_SELECTION"
  if ! normalized="$(PYTHONPATH="$SOURCE_ROOT" python3 - "$excluded_csv" <<'PY'
import sys

try:
    from services.platform.settings import FEATURE_FLAG_KEYS
except Exception as exc:
    print(f"feature catalog unavailable: {exc}", file=sys.stderr)
    raise SystemExit(2)

excluded = {item.strip() for item in str(sys.argv[1] or "").split(",") if item.strip()}
enabled = [key for key in FEATURE_FLAG_KEYS if key not in excluded]
print(",".join(enabled))
PY
)"; then
    return 1
  fi
  NORMALIZED_FEATURE_SELECTION="$normalized"
  return 0
}

prompt_feature_exclusion_scope() {
  local answer
  say "Disable services/packages:"
  print_known_feature_bundles
  print_known_feature_keys
  say "Enter comma-separated package names, b-numbers, f-numbers, or feature keys to disable. Blank keeps every feature enabled."
  while true; do
    prompt_value "Disabled feature packages / keys" "" answer
    if [[ -z "${answer:-}" || -z "${answer//[[:space:]]/}" ]]; then
      FEATURE_MODE="all"
      FEATURE_LIST=""
      FEATURE_BUNDLES=""
      FEATURE_LIST_FINALIZED=0
      say "Feature exclusions: none; every feature flag stays enabled."
      return 0
    fi
    if normalize_feature_exclusion_selection "$answer"; then
      FEATURE_MODE="custom"
      FEATURE_BUNDLES=""
      FEATURE_LIST="$NORMALIZED_FEATURE_SELECTION"
      FEATURE_LIST_FINALIZED=1
      say "Feature exclusions: $answer"
      say "Resolved enabled feature keys after subtraction: ${FEATURE_LIST:-<none>}"
      return 0
    fi
    say "Please enter valid package names, b-numbers, f-numbers, or feature keys."
  done
}

prompt_feature_bundle_scope() {
  local answer
  say "Feature packages:"
  print_known_feature_bundles
  say "Enter comma-separated package numbers or names. Examples: ops-minimum,safe-community,exchange-ops or social,storage,creator-media."
  prompt_value "Feature packages" "${FEATURE_BUNDLES:-full-user}" answer
  FEATURE_BUNDLES="$answer"
  normalize_feature_or_bundle_selection "$FEATURE_BUNDLES" "bundle" || die "invalid feature bundle selection: $FEATURE_BUNDLES"
  FEATURE_LIST="$NORMALIZED_FEATURE_SELECTION"
  say "Resolved feature package keys: ${FEATURE_LIST:-<none>}"
}

prompt_token_feature_scope() {
  local answer
  say "Generated dev token allowed feature scope:"
  say "   0) unrestricted token scope (default; no token-level feature restriction)"
  print_known_feature_bundles
  print_known_feature_keys
  say "Enter comma-separated package names, b-numbers, f-numbers, or feature keys. Examples: safe-community,storage,exchange-ops or b8,feature_videos_enabled,f20."
  while true; do
    prompt_value "Generated dev token allowed feature packages / keys" "$DEV_TOKEN_FEATURES" answer
    if normalize_token_feature_selection "$answer"; then
      DEV_TOKEN_FEATURES="$NORMALIZED_DEV_TOKEN_FEATURES"
      if [[ -z "$DEV_TOKEN_FEATURES" ]]; then
        say "Generated dev token feature scope: unrestricted"
      else
        say "Generated dev token feature scope: $DEV_TOKEN_FEATURES"
      fi
      return 0
    fi
    say "Please choose listed numbers, feature_* keys, short names like chat/videos, or 0 for unrestricted."
  done
}

prompt_server_mode() {
  local choice
  normalize_server_mode
  say "Server mode:"
  say "  1) dev_ready"
  say "  2) internal_test"
  say "  3) test"
  say "  4) preprod"
  say "  5) production"
  say "  6) maintenance"
  say "  7) incident_lockdown"
  say "  8) superweak"
  while true; do
    printf 'Server mode [%s]: ' "$SERVER_MODE"
    if ! read -r choice; then
      die "interactive setup was interrupted"
    fi
    choice="${choice:-$SERVER_MODE}"
    case "${choice,,}" in
      1|dev|development|dev_ready)
        SERVER_MODE="dev_ready"
        return 0
        ;;
      2|internal|internal_test)
        SERVER_MODE="internal_test"
        return 0
        ;;
      3|test)
        SERVER_MODE="test"
        return 0
        ;;
      4|preprod)
        SERVER_MODE="preprod"
        return 0
        ;;
      5|production|prod)
        SERVER_MODE="production"
        return 0
        ;;
      6|maintenance)
        SERVER_MODE="maintenance"
        return 0
        ;;
      7|incident_lockdown|incident|lockdown)
        SERVER_MODE="incident_lockdown"
        return 0
        ;;
      8|superweak)
        SERVER_MODE="superweak"
        return 0
        ;;
      *)
        say "Please choose a listed mode."
        ;;
    esac
  done
}

prompt_extra_accounts() {
  local add_accounts=0
  local username
  local password
  local role

  prompt_yes_no "Create additional dev accounts" 0 add_accounts
  if [[ "$add_accounts" != "1" ]]; then
    return 0
  fi

  while true; do
    prompt_value "Extra account username (blank to finish)" "" username
    if [[ -z "$username" ]]; then
      return 0
    fi
    prompt_value "Password for $username" "test" password
    prompt_value "Role for $username (user/manager/super_admin)" "user" role
    append_csv_value EXTRA_ACCOUNTS "$username:$password:$role"
  done
}

prompt_launch_layout() {
  local default_choice="1"
  local answer

  normalize_yes_no_value "$IN_PLACE" "in-place"
  IN_PLACE="$NORMALIZED_YES_NO"
  normalize_yes_no_value "$RUNTIME_IN_SOURCE" "runtime in source"
  RUNTIME_IN_SOURCE="$NORMALIZED_YES_NO"
  if [[ "$RUNTIME_IN_SOURCE" == "1" ]]; then
    default_choice="3"
  elif [[ "$IN_PLACE" == "1" ]]; then
    default_choice="2"
  fi

  say "Launch layout:"
  say "  1) isolated tmp copy + tmp runtime (best for QA; no repo runtime changes)"
  say "  2) current repo + tmp runtime (no source copy; runtime stays under --run-root)"
  say "  3) current repo + ./runtime (local deployment layout)"
  say "  4) current repo + custom runtime directory/path"
  while true; do
    printf 'Choose launch layout [default %s]: ' "$default_choice"
    if ! read -r answer; then
      die "interactive setup was interrupted"
    fi
    answer="${answer:-$default_choice}"
    case "${answer,,}" in
      1|tmp|copy|isolated|qa)
        IN_PLACE=0
        RUNTIME_IN_SOURCE=0
        return 0
        ;;
      2|current|in-place|inplace|no-copy|nocopy)
        IN_PLACE=1
        RUNTIME_IN_SOURCE=0
        return 0
        ;;
      3|deploy|deployment|source|source-runtime|runtime-in-source|formal)
        IN_PLACE=1
        RUNTIME_IN_SOURCE=1
        return 0
        ;;
      4|custom|custom-runtime|runtime-root|runtime-dir|runtime-directory)
        IN_PLACE=1
        RUNTIME_IN_SOURCE=0
        prompt_value "Custom runtime directory/path" "$CUSTOM_RUNTIME_ROOT" CUSTOM_RUNTIME_ROOT
        [[ -n "$CUSTOM_RUNTIME_ROOT" ]] || die "custom runtime directory/path cannot be blank when launch layout 4 is selected"
        CUSTOM_RUNTIME_ROOT_PROMPTED=1
        return 0
        ;;
      *)
        say "Please choose 1, 2, 3, or 4."
        ;;
    esac
  done
}

prompt_server_runner() {
  local default_choice="1"
  local answer
  local customize=0
  local run_capacity_probe=0

  normalize_server_runner
  if [[ "$SERVER_RUNNER" == "flask" ]]; then
    default_choice="2"
  fi

  say "Server runner:"
  say "  1) bounded gunicorn (recommended; protects the app under uploads/HLS/load)"
  say "     - imports server:app; does not run server.py __main__ or legacy in-process workers"
  say "  2) Flask/Werkzeug direct server (debug only; not for upload/HLS stress)"
  say "     - same path as python3 server.py; starts legacy in-process workers in one process"
  while true; do
    printf 'Choose server runner [default %s]: ' "$default_choice"
    if ! read -r answer; then
      die "interactive setup was interrupted"
    fi
    answer="${answer:-$default_choice}"
    case "${answer,,}" in
      1|gunicorn|bounded|wsgi|prod|production)
        SERVER_RUNNER="gunicorn"
        break
        ;;
      2|flask|werkzeug|direct|debug)
        SERVER_RUNNER="flask"
        return 0
        ;;
      *)
        say "Please choose 1 or 2."
        ;;
    esac
  done

  normalize_capacity_probe_mode
  normalize_capacity_probe_tier
  if [[ "$CAPACITY_PROBE_MODE" != "force" ]]; then
    prompt_capacity_defaults_source
  fi
  if [[ "$CAPACITY_PROBE_MODE" == "force" ]]; then
    if [[ "$CAPACITY_PROBE_TIER" == "auto" ]]; then
      prompt_capacity_probe_tier
    fi
    run_capacity_probe_for_defaults
  elif [[ "$CAPACITY_SETTINGS_FINALIZED" != "1" && -f "$CAPACITY_DEFAULTS_FILE" ]]; then
    prompt_yes_no "Retest local capacity before launch (existing .hackme_capacity_defaults.env will be reused if no)" 0 run_capacity_probe
    if [[ "$run_capacity_probe" == "1" ]]; then
      CAPACITY_PROBE_MODE="force"
      if [[ "$CAPACITY_PROBE_TIER" == "auto" ]]; then
        prompt_capacity_probe_tier
      fi
      run_capacity_probe_for_defaults
    fi
  elif [[ "$CAPACITY_SETTINGS_FINALIZED" != "1" ]] && gunicorn_capacity_auto_requested && [[ "$CAPACITY_PROBE_MODE" != "never" ]]; then
    prompt_yes_no "No local capacity result found. Run capacity probe for auto settings now" 1 run_capacity_probe
    if [[ "$run_capacity_probe" == "1" ]]; then
      if [[ "$CAPACITY_PROBE_TIER" == "auto" ]]; then
        prompt_capacity_probe_tier
      fi
      run_capacity_probe_for_defaults
    else
      CAPACITY_PROBE_MODE="never"
    fi
  fi

  if [[ "$CAPACITY_SETTINGS_FINALIZED" != "1" ]]; then
    prompt_yes_no "Customize gunicorn worker/thread settings" 0 customize
  fi
  if [[ "$customize" == "1" ]]; then
    prompt_value "Gunicorn workers" "$GUNICORN_WORKERS" GUNICORN_WORKERS
    prompt_value "Gunicorn threads per worker" "$GUNICORN_THREADS" GUNICORN_THREADS
    prompt_value "Gunicorn timeout seconds" "$GUNICORN_TIMEOUT" GUNICORN_TIMEOUT
    prompt_value "Gunicorn backlog" "$GUNICORN_BACKLOG" GUNICORN_BACKLOG
    CAPACITY_SETTINGS_FINALIZED=1
  fi
  prompt_hls_slot_probe
}

prompt_runtime_config() {
  local default_run_root="${RUN_ROOT:-/tmp/hackme_web_dev_${RUN_ID}_$$}"
  local use_default_passwords=1

  if [[ ! -t 0 || ! -t 1 ]]; then
    die "interactive setup requires a TTY; pass --cli to use command/env options without prompts"
  fi

  normalize_yes_no_value "$SECURITY_SETTINGS_ENABLED" "security"
  SECURITY_SETTINGS_ENABLED="$NORMALIZED_YES_NO"
  normalize_yes_no_value "$BTC_TRADE_AUTOSTART" "btc trade autostart"
  BTC_TRADE_AUTOSTART="$NORMALIZED_YES_NO"

  say "[dev-tmp] interactive setup; pass --cli to skip prompts"
  prompt_launch_layout
  if [[ "$RUNTIME_IN_SOURCE" != "1" && !( "$IN_PLACE" == "1" && -n "$CUSTOM_RUNTIME_ROOT" ) ]]; then
    prompt_value "Tmp workspace/run root" "$default_run_root" RUN_ROOT
  fi
  prompt_value "Cloud drive actual storage root (blank = runtime/storage)" "$CLOUD_DRIVE_STORAGE_ROOT" CLOUD_DRIVE_STORAGE_ROOT
  prompt_value "Cloud drive max occupancy (MB or 10G; blank = keep app default, -1 = disk 95%)" "$CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB" CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB
  prompt_value "Host" "$HOST" HOST
  prompt_value "Port" "$PORT" PORT
  prompt_yes_no "Disable trusted-host checks for dev convenience" "$DISABLE_TRUSTED_HOSTS" DISABLE_TRUSTED_HOSTS
  prompt_server_runner
  prompt_remote_download_settings
  prompt_yes_no "Skip dependency install / reuse existing environment" "$SKIP_INSTALL" SKIP_INSTALL
  prompt_feature_settings
  prompt_yes_no "Enable security settings" "$SECURITY_SETTINGS_ENABLED" SECURITY_SETTINGS_ENABLED
  prompt_value "Idle logout countdown minutes (blank = selected security profile default, 0 = disabled)" "$SESSION_IDLE_TIMEOUT_MINUTES" SESSION_IDLE_TIMEOUT_MINUTES
  prompt_server_mode
  if [[ "$SERVER_MODE" == "dev_ready" ]]; then
    prompt_yes_no "Enable trading background jobs in dev_ready (mutates trading state)" "$TRADING_BACKGROUND_DEV_READY" TRADING_BACKGROUND_DEV_READY
  fi
  prompt_requirements_from_features
  if [[ "$SERVER_MODE" == "test" || "$SERVER_MODE" == "internal_test" ]]; then
    prompt_value "Generated dev token TTL minutes" "$DEV_TOKEN_TTL_MINUTES" DEV_TOKEN_TTL_MINUTES
    prompt_token_feature_scope
    prompt_value "Generated dev token account username" "$DEV_TOKEN_USER" DEV_TOKEN_USER
    prompt_value "Password for generated token account (blank = keep existing or auto-generate new account)" "$DEV_TOKEN_PASSWORD" DEV_TOKEN_PASSWORD
    prompt_value "Role for generated token account (user/manager/super_admin)" "$DEV_TOKEN_ROLE" DEV_TOKEN_ROLE
  fi
  prompt_yes_no "Run server in foreground" "$FOREGROUND" FOREGROUND
  prompt_yes_no "Start BTC_trade background job after boot" "$BTC_TRADE_AUTOSTART" BTC_TRADE_AUTOSTART

  if [[ "$ROOT_PASSWORD" != "root" || "$MANAGER_PASSWORD" != "admin" || "$TEST_PASSWORD" != "test" ]]; then
    use_default_passwords=0
  fi
  prompt_yes_no "Use default dev account passwords (root/root admin/admin test/test)" "$use_default_passwords" use_default_passwords
  if [[ "$use_default_passwords" == "1" ]]; then
    ROOT_PASSWORD="root"
    MANAGER_PASSWORD="admin"
    TEST_PASSWORD="test"
  else
    prompt_value "Root password" "$ROOT_PASSWORD" ROOT_PASSWORD
    prompt_value "Manager password" "$MANAGER_PASSWORD" MANAGER_PASSWORD
    prompt_value "Test password" "$TEST_PASSWORD" TEST_PASSWORD
  fi
  prompt_extra_accounts
}

copy_repo() {
  mkdir -p "$COPY_ROOT"
  # The tmp runtime only needs files required to run, develop, smoke-test, and
  # validate release gates. Copy from an allowlist so reference repos/deploy examples/git
  # metadata and future large non-runtime artifacts never inflate isolated
  # workspaces. Keep docs because RC/operational gates assert release
  # scope and runbook files from the copied runtime.
  local copy_items=(
    "server.py"
    "bootstrap.schema.sql"
    "pytest.ini"
    "requirements.txt"
    "requirements-dev.txt"
    "requirements-comfyui.txt"
    "requirements-games.txt"
    "requirements-hf.txt"
    "requirements-minimal.txt"
    "test_for_develop.sh"
    "docs"
    "public"
    "routes"
    "scripts"
    "services"
    "tests"
    "workflows"
  )
  local existing_items=()
  local item
  for item in "${copy_items[@]}"; do
    if [[ -e "$SOURCE_ROOT/$item" ]]; then
      existing_items+=("$item")
    fi
  done
  tar -C "$SOURCE_ROOT" \
    --exclude='*.log' \
    --exclude='*.out' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='.coverage' \
    --exclude='*/reports' \
    --exclude='*/.pytest_cache' \
    --exclude='*/.ruff_cache' \
    --exclude='*/.mypy_cache' \
    --exclude='*/__pycache__' \
    --exclude='*/cache' \
    --exclude='*/runtime' \
    -cf - "${existing_items[@]}" | tar -C "$COPY_ROOT" -xf -
  find "$COPY_ROOT/scripts" "$COPY_ROOT/tests" -type f -name '*.md' -delete 2>/dev/null || true
}

ensure_official_workflows_source() {
  local root="$1"
  local workflow_dir="$root/workflows/comfyui"
  local count
  count="$(find "$workflow_dir" -mindepth 2 -maxdepth 2 -name workflow.json 2>/dev/null | wc -l | tr -d ' ')"
  if [[ "$count" == "0" ]]; then
    die "official ComfyUI workflow bundles are missing under $workflow_dir; dev runtime cannot seed default official workflows"
  fi
  say "[dev-tmp] workflows: found $count official ComfyUI workflow bundle(s)"
}

python_has_runtime_dependencies() {
  python3 - <<'PY' >/dev/null 2>&1 || return 1
import argon2
import cryptography
import flask
import flask_talisman
import chess
import websocket
PY
  if [[ "${SERVER_RUNNER:-flask}" == "gunicorn" ]]; then
    python3 - <<'PY' >/dev/null 2>&1 || return 1
import gunicorn
PY
  fi
  return 0
}

resolve_python() {
  local venv_dir="$RUNTIME_ROOT/venv"
  if [[ -x "$venv_dir/bin/python3" ]]; then
    PYTHON_BIN="$venv_dir/bin/python3"
    return 0
  fi
  if [[ "$SKIP_INSTALL" == "1" ]]; then
    if python_has_runtime_dependencies; then
      PYTHON_BIN="python3"
      return 0
    fi
    die "--skip-install requires either an existing tmp venv at $venv_dir or a ready system python3 environment"
  fi
  if python_has_runtime_dependencies; then
    PYTHON_BIN="python3"
    return 0
  fi
  python3 -m venv "$venv_dir"
  PYTHON_BIN="$venv_dir/bin/python3"
  if [[ ! -x "$PYTHON_BIN" ]]; then
    die "failed to create tmp venv at $venv_dir"
  fi
  local requirements_file
  local requirements_path
  normalize_requirements_files "$REQUIREMENTS_FILE" || die "invalid requirements file selection: $REQUIREMENTS_FILE"
  REQUIREMENTS_FILE="$NORMALIZED_REQUIREMENTS_FILES"
  PIP_DISABLE_PIP_VERSION_CHECK=1 "$PYTHON_BIN" -m pip install --upgrade pip
  for requirements_file in $REQUIREMENTS_FILE; do
    requirements_path="$COPY_ROOT/$requirements_file"
    [[ -f "$requirements_path" ]] || die "requirements file not found in copied runtime: $requirements_file"
    say "[dev-tmp] installing dependencies from $requirements_file - $(requirements_file_description "$requirements_file")"
    PIP_DISABLE_PIP_VERSION_CHECK=1 "$PYTHON_BIN" -m pip install -r "$requirements_path"
  done
}

migrate_legacy_runtime_storage_to_cloud_drive_root() {
  local legacy_root="$RUNTIME_ROOT/storage"
  if [[ -z "$CLOUD_DRIVE_STORAGE_ROOT" ]]; then
    return 0
  fi
  if [[ ! -d "$legacy_root" ]]; then
    return 0
  fi
  if [[ "$(cd "$legacy_root" && pwd -P)" == "$(cd "$EFFECTIVE_STORAGE_ROOT" && pwd -P)" ]]; then
    return 0
  fi
  "$PYTHON_BIN" - "$legacy_root" "$EFFECTIVE_STORAGE_ROOT" <<'PY'
import shutil
import sys
from pathlib import Path

source = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2]).resolve()
copied = 0
skipped_existing = 0
skipped_special = 0

if not source.exists() or not source.is_dir():
    raise SystemExit(0)
destination.mkdir(parents=True, exist_ok=True)
for item in source.rglob("*"):
    try:
        relative = item.relative_to(source)
    except ValueError:
        continue
    target = destination / relative
    if item.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        continue
    if item.is_symlink() or not item.is_file():
        skipped_special += 1
        continue
    if target.exists():
        skipped_existing += 1
        continue
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(item, target)
    copied += 1

if copied or skipped_existing or skipped_special:
    print(
        "[dev-tmp] storage migration: "
        f"copied={copied} skipped_existing={skipped_existing} "
        f"skipped_special={skipped_special} source={source} destination={destination}",
        flush=True,
    )
PY
}

server_probe_host() {
  case "$HOST" in
    0.0.0.0|::|"[::]")
      printf '%s\n' "127.0.0.1"
      ;;
    *)
      printf '%s\n' "$HOST"
      ;;
  esac
}

wait_for_server_url() {
  command -v curl >/dev/null 2>&1 || return 1
  local url
  local scheme
  local probe_host
  probe_host="$(server_probe_host)"
  for _ in $(seq 1 80); do
    for scheme in https http; do
      url="${scheme}://${probe_host}:${PORT}/api/version"
      if curl -k -sS "$url" >/dev/null 2>&1; then
        printf '%s\n' "${scheme}://${probe_host}:${PORT}"
        return 0
      fi
    done
    sleep 0.5
  done
  return 1
}

print_generated_dev_tokens() {
  local tokens_file="${HACKME_DEV_TOKENS_FILE:-}"
  if [[ -z "$tokens_file" || ! -s "$tokens_file" ]]; then
    return 0
  fi
  say "[dev-tmp] tokens:    $tokens_file"
  "$PYTHON_BIN" - "$tokens_file" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
except Exception as exc:
    print(f"[dev-tmp] token read failed: {exc}")
    raise SystemExit(0)

tokens = payload.get("tokens") if isinstance(payload, dict) else {}
if not isinstance(tokens, dict):
    raise SystemExit(0)
token_user = payload.get("token_user") if isinstance(payload, dict) else {}
if isinstance(token_user, dict) and token_user.get("username"):
    created_text = "created" if token_user.get("created") else "existing"
    print(f"[dev-tmp] token user: {token_user.get('username')} ({created_text}, role={token_user.get('role') or 'user'})")
    if token_user.get("password"):
        print(f"[dev-tmp]   password={token_user.get('password')}")
feature_scope = payload.get("token_feature_scope") or "unknown"
allowed_keys = payload.get("allowed_feature_keys") or []
print(f"[dev-tmp] token feature scope: {feature_scope}")
if allowed_keys:
    print(f"[dev-tmp] token allowed feature keys: {', '.join(str(item) for item in allowed_keys)}")
feature_catalog = payload.get("available_feature_keys") or []
if feature_catalog:
    print("[dev-tmp] available feature keys:")
    for item in feature_catalog:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        label = str(item.get("label") or key).strip()
        enabled = "on" if item.get("enabled") else "off"
        allowed = "allowed" if item.get("allowed_by_token") else "blocked"
        print(f"[dev-tmp]   {key} [{enabled}/{allowed}] - {label}")
for name, info in tokens.items():
    if not isinstance(info, dict):
        continue
    token = str(info.get("token") or "").strip()
    if not token:
        continue
    username = info.get("username") or info.get("target_username") or "test"
    expires_at = info.get("expires_at") or ""
    features = info.get("allowed_features") or []
    feature_text = "unrestricted" if not features else ",".join(str(item) for item in features)
    print(f"[dev-tmp] {name}: {token}")
    print(f"[dev-tmp]   user={username} expires_at={expires_at} features={feature_text}")
for warning in payload.get("warnings") or []:
    print(f"[dev-tmp] token warning: {warning}")
PY
}

runtime_backup_default_archive() {
  local parent
  parent="$(dirname "$RUNTIME_ROOT")"
  printf '%s\n' "$parent/hackme_runtime_backup_${RUN_ID}.tar.gz"
}

path_is_inside_runtime() {
  local candidate="$1"
  "$PYTHON_BIN" - "$RUNTIME_ROOT" "$candidate" <<'INNERPY'
from pathlib import Path
import sys
root = Path(sys.argv[1]).resolve()
path = Path(sys.argv[2]).expanduser().resolve(strict=False)
try:
    path.relative_to(root)
except ValueError:
    raise SystemExit(1)
raise SystemExit(0)
INNERPY
}

validate_restore_archive_members() {
  local archive="$1"
  "$PYTHON_BIN" - "$archive" <<'INNERPY'
import posixpath
import sys
import tarfile
archive = sys.argv[1]
try:
    with tarfile.open(archive, "r:gz") as tf:
        for member in tf.getmembers():
            name = str(member.name or "").strip()
            if not name:
                continue
            normalized = posixpath.normpath(name)
            if normalized.startswith("/") or normalized == ".." or normalized.startswith("../") or "/../" in normalized:
                print(f"unsafe archive member: {name}", file=sys.stderr)
                raise SystemExit(1)
except tarfile.TarError as exc:
    print(f"invalid tar archive: {exc}", file=sys.stderr)
    raise SystemExit(1)
INNERPY
}

runtime_archive_is_safe_path() {
  local archive="$1"
  [[ -n "$archive" ]] || return 1
  case "$archive" in
    *.tar.gz|*.tgz) return 0 ;;
  esac
  return 1
}

runtime_root_has_backup_state() {
  [[ -d "$RUNTIME_ROOT" ]] || return 1
  local item
  for item in database chats anchors reports dev_tokens.json server.pid; do
    if [[ -e "$RUNTIME_ROOT/$item" ]]; then
      return 0
    fi
  done
  find "$RUNTIME_ROOT" -mindepth 1 -maxdepth 2 \
    ! -path "$RUNTIME_ROOT/storage" \
    ! -path "$RUNTIME_ROOT/storage/*" \
    ! -path "$RUNTIME_ROOT/venv" \
    ! -path "$RUNTIME_ROOT/venv/*" \
    ! -path "$RUNTIME_ROOT/logs" \
    ! -path "$RUNTIME_ROOT/logs/*" \
    ! -path "$RUNTIME_ROOT/pycache" \
    ! -path "$RUNTIME_ROOT/pycache/*" \
    ! -path "$RUNTIME_ROOT/tmp" \
    ! -path "$RUNTIME_ROOT/tmp/*" \
    ! -path "$RUNTIME_ROOT/temp" \
    ! -path "$RUNTIME_ROOT/temp/*" \
    -print -quit 2>/dev/null | grep -q .
}

backup_runtime_state() {
  local archive="$1"
  if [[ ! -d "$RUNTIME_ROOT" ]]; then
    die "runtime root does not exist; cannot backup: $RUNTIME_ROOT"
  fi
  if ! runtime_root_has_backup_state; then
    die "runtime root has no recognizable runtime state to backup: $RUNTIME_ROOT (pass --runtime-root PATH for the runtime directory; --backup PATH is the output archive or directory)"
  fi
  if [[ -n "$archive" && -d "$archive" ]]; then
    archive="$archive/hackme_runtime_backup_${RUN_ID}.tar.gz"
  fi
  [[ -n "$archive" ]] || archive="$(runtime_backup_default_archive)"
  runtime_archive_is_safe_path "$archive" || die "backup archive must end with .tar.gz or .tgz, or be an existing directory: $archive"
  if path_is_inside_runtime "$archive"; then
    die "backup archive must be outside runtime root: $archive"
  fi
  mkdir -p "$(dirname "$archive")"
  if [[ -e "$archive" ]]; then
    die "backup archive already exists: $archive"
  fi
  say "[dev-tmp] backup:   runtime=$RUNTIME_ROOT"
  say "[dev-tmp] backup:   archive=$archive"
  say "[dev-tmp] backup:   excluding storage/, venv/, pycache/, logs/, pid/cache/temp files"
  tar -C "$RUNTIME_ROOT" \
    --exclude='./storage' \
    --exclude='./venv' \
    --exclude='./pycache' \
    --exclude='./logs' \
    --exclude='./server.pid' \
    --exclude='./*.sock' \
    --exclude='./*.lock' \
    --exclude='./__pycache__' \
    --exclude='./tmp' \
    --exclude='./temp' \
    -czf "$archive" .
  say "[dev-tmp] backup:   created $archive"
}

restore_runtime_state() {
  local archive="$1"
  [[ -n "$archive" ]] || die "--restore requires a backup archive path"
  [[ -f "$archive" ]] || die "restore archive not found: $archive"
  runtime_archive_is_safe_path "$archive" || die "restore archive must end with .tar.gz or .tgz: $archive"
  validate_restore_archive_members "$archive" || die "restore archive contains unsafe paths: $archive"
  if path_is_inside_runtime "$archive"; then
    die "restore archive must be outside runtime root: $archive"
  fi
  ensure_runtime_not_running "restore"
  mkdir -p "$(dirname "$RUNTIME_ROOT")"
  local stamp backup_existing
  stamp="$(date +%Y%m%d_%H%M%S)"
  backup_existing="${RUNTIME_ROOT}.pre-restore-${stamp}"
  say "[dev-tmp] restore:  runtime=$RUNTIME_ROOT"
  say "[dev-tmp] restore:  archive=$archive"
  say "[dev-tmp] restore:  storage/ is not restored by this archive format"
  if [[ -e "$RUNTIME_ROOT" ]]; then
    [[ ! -e "$backup_existing" ]] || die "pre-restore path already exists: $backup_existing"
    mv "$RUNTIME_ROOT" "$backup_existing"
    say "[dev-tmp] restore:  moved existing runtime to $backup_existing"
  fi
  mkdir -p "$RUNTIME_ROOT"
  tar -C "$RUNTIME_ROOT" -xzf "$archive"
  mkdir -p "$RUNTIME_ROOT/database" "$RUNTIME_ROOT/logs" "$RUNTIME_ROOT/chats" "$RUNTIME_ROOT/anchors" "$RUNTIME_ROOT/reports"
  say "[dev-tmp] restore:  restored runtime state"
}

process_uses_runtime_root() {
  local pid="$1"
  local runtime_real cwd env_runtime args
  [[ -n "$pid" && -r "/proc/$pid/cmdline" ]] || return 1
  runtime_real="$(readlink -f "$RUNTIME_ROOT" 2>/dev/null || true)"
  [[ -n "$runtime_real" ]] || return 1
  args="$(tr '\000' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  if [[ "$args" == *"$runtime_real"* || "$cwd" == "$runtime_real" || "$cwd" == "$runtime_real"/* ]]; then
    return 0
  fi
  if [[ -r "/proc/$pid/environ" ]]; then
    env_runtime="$(tr '\000' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^HACKME_RUNTIME_DIR=//p' | tail -n 1)"
    env_runtime="$(readlink -f "$env_runtime" 2>/dev/null || true)"
    if [[ "$env_runtime" == "$runtime_real" ]]; then
      return 0
    fi
  fi
  return 1
}

ensure_runtime_not_running() {
  local action="$1"
  local candidates=()
  local pid
  if [[ -r "$PID_FILE" ]]; then
    pid="$(sed -n '1p' "$PID_FILE" 2>/dev/null | tr -dc '0-9')"
    [[ -n "$pid" ]] && append_unique_array_value candidates "$pid"
  fi
  while IFS= read -r pid; do
    [[ -n "$pid" ]] && append_unique_array_value candidates "$pid"
  done < <(scan_dev_server_pids 2>/dev/null || true)
  for pid in "${candidates[@]:-}"; do
    kill -0 "$pid" 2>/dev/null || continue
    if process_uses_runtime_root "$pid"; then
      die "$action refused because runtime appears active (pid $pid). Run --stop --port $PORT first: $RUNTIME_ROOT"
    fi
  done
}

write_reset_orphan_recovery_bundle() {
  local bundle_root bundle_dir secrets_dir scripts_dir readme_path
  [[ -n "${EFFECTIVE_STORAGE_ROOT:-}" ]] || return 0
  bundle_root="$EFFECTIVE_STORAGE_ROOT/.reset_orphan_recovery"
  bundle_dir="$bundle_root/reset_${RUN_ID}"
  if [[ -e "$bundle_dir" ]]; then
    die "reset orphan recovery bundle already exists: $bundle_dir"
  fi
  mkdir -p "$bundle_dir" "$bundle_dir/database" "$bundle_dir/runtime_secrets" "$bundle_dir/scripts/admin" "$bundle_dir/orphaned_storage"
  chmod 700 "$bundle_dir" "$bundle_dir/runtime_secrets" 2>/dev/null || true

  if [[ -d "$RUNTIME_ROOT/database" ]]; then
    cp -a "$RUNTIME_ROOT/database/." "$bundle_dir/database/"
  fi

  local secret_name secret_path
  for secret_name in \
    .filekey \
    .fkey \
    .csrfkey \
    .integrity_key \
    .chain_seed \
    .server_mode_log_hmac_key \
    cert.pem \
    key.pem \
    integrity_manifest.json
  do
    secret_path="$RUNTIME_ROOT/$secret_name"
    if [[ -e "$secret_path" ]]; then
      cp -a "$secret_path" "$bundle_dir/runtime_secrets/$secret_name"
    fi
  done
  chmod 600 "$bundle_dir/runtime_secrets"/* 2>/dev/null || true

  if [[ -f "$SOURCE_ROOT/scripts/admin/decrypt_server_files.py" ]]; then
    cp -a "$SOURCE_ROOT/scripts/admin/decrypt_server_files.py" "$bundle_dir/scripts/admin/decrypt_server_files.py"
  fi

  local storage_item storage_basename
  shopt -s dotglob nullglob
  for storage_item in "$EFFECTIVE_STORAGE_ROOT"/*; do
    [[ -e "$storage_item" ]] || continue
    storage_basename="$(basename "$storage_item")"
    [[ "$storage_basename" == ".reset_orphan_recovery" ]] && continue
    mv "$storage_item" "$bundle_dir/orphaned_storage/"
  done
  shopt -u dotglob nullglob

  printf '%s\n' "$RUNTIME_ROOT" > "$bundle_dir/runtime_root.txt"
  printf '%s\n' "$EFFECTIVE_STORAGE_ROOT" > "$bundle_dir/storage_root.txt"
  cat > "$bundle_dir/export_server_encrypted_plaintext.sh" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
BUNDLE_DIR=\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)
ACTION_LOCK="\$BUNDLE_DIR/recovery_action.lock"
SOURCE_ROOT="$SOURCE_ROOT"
PYTHON_BIN="$PYTHON_BIN"
BUNDLE_DATABASE="$bundle_dir/database"
BUNDLE_STORAGE="$bundle_dir/orphaned_storage"
BUNDLE_KEY="$bundle_dir/runtime_secrets/.filekey"
OUTPUT_DIR="\${1:-}"

if [[ -e "\$ACTION_LOCK" ]]; then
  echo "[orphan-export] refusing: recovery action already selected: \$(cat "\$ACTION_LOCK")" >&2
  exit 1
fi
if [[ -z "\$OUTPUT_DIR" ]]; then
  printf 'Plaintext output directory: '
  if ! read -r OUTPUT_DIR; then
    echo "[orphan-export] interrupted" >&2
    exit 1
  fi
fi
if [[ -z "\$OUTPUT_DIR" ]]; then
  echo "[orphan-export] output directory is required" >&2
  exit 1
fi
resolved_output=\$(mkdir -p "\$OUTPUT_DIR" && cd "\$OUTPUT_DIR" && pwd)
case "\$resolved_output" in
  "\$BUNDLE_DIR"|"\$BUNDLE_DIR"/*|"$EFFECTIVE_STORAGE_ROOT"|"$EFFECTIVE_STORAGE_ROOT"/*)
    echo "[orphan-export] refusing: plaintext output must be outside the recovery bundle and live storage root" >&2
    exit 1
    ;;
esac
printf 'plaintext_export\nstatus=started\noutput_dir=%s\nat=%s\n' "\$resolved_output" "\$(date -Is)" > "\$ACTION_LOCK"
chmod 600 "\$ACTION_LOCK" 2>/dev/null || true
echo "[orphan-export] recovery action locked to plaintext export; catalog restore is now refused."
PYTHONPATH="\$SOURCE_ROOT" "\$PYTHON_BIN" "\$SOURCE_ROOT/scripts/admin/decrypt_server_files.py" \\
  --db "\$BUNDLE_DATABASE/database.db" \\
  --storage-root "\$BUNDLE_STORAGE" \\
  --key-file "\$BUNDLE_KEY" \\
  --output-dir "\$resolved_output" \\
  --confirm-plaintext-output
printf 'status=completed\ncompleted_at=%s\n' "\$(date -Is)" >> "\$ACTION_LOCK"
EOF
  chmod 700 "$bundle_dir/export_server_encrypted_plaintext.sh" 2>/dev/null || true
  cat > "$bundle_dir/restore_database_catalog_from_bundle.sh" <<EOF
#!/usr/bin/env bash
set -Eeuo pipefail
BUNDLE_DIR=\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)
ACTION_LOCK="\$BUNDLE_DIR/recovery_action.lock"
RUNTIME_ROOT="$RUNTIME_ROOT"
BUNDLE_DATABASE="$bundle_dir/database"
BUNDLE_STORAGE="$bundle_dir/orphaned_storage"
TARGET_DATABASE="$RUNTIME_ROOT/database"
TARGET_STORAGE="$EFFECTIVE_STORAGE_ROOT"
STAMP=\$(date +%Y%m%d_%H%M%S)
BACKUP_DATABASE="$RUNTIME_ROOT/database.before-orphan-catalog-restore-\$STAMP"
BACKUP_STORAGE="$bundle_dir/post_reset_storage_backup_\$STAMP"
STAGED_DATABASE="$bundle_dir/.restore_database_stage_\$STAMP"
cleanup_restore_stage() {
  rm -rf "\$STAGED_DATABASE"
}
trap cleanup_restore_stage EXIT

if [[ -e "\$ACTION_LOCK" ]]; then
  echo "[orphan-restore] refusing: recovery action already selected: \$(cat "\$ACTION_LOCK")" >&2
  exit 1
fi
if pgrep -af 'gunicorn|server:app' >/dev/null 2>&1; then
  echo "[orphan-restore] refusing: a server process appears to be running. Stop it before restoring catalog metadata." >&2
  exit 1
fi
if [[ ! -d "\$BUNDLE_DATABASE" ]]; then
  echo "[orphan-restore] missing bundled database metadata: \$BUNDLE_DATABASE" >&2
  exit 1
fi
if [[ ! -d "\$BUNDLE_STORAGE" ]]; then
  echo "[orphan-restore] missing bundled orphaned storage: \$BUNDLE_STORAGE" >&2
  exit 1
fi
mkdir -p "\$RUNTIME_ROOT" "\$TARGET_STORAGE"
if [[ -e "\$BACKUP_DATABASE" ]]; then
  echo "[orphan-restore] backup path already exists: \$BACKUP_DATABASE" >&2
  exit 1
fi
if [[ -e "\$STAGED_DATABASE" ]]; then
  echo "[orphan-restore] stage path already exists: \$STAGED_DATABASE" >&2
  exit 1
fi
mkdir -p "\$STAGED_DATABASE"
cp -a "\$BUNDLE_DATABASE/." "\$STAGED_DATABASE/"
repair_missing_file_catalog_owners() {
  local db="\$STAGED_DATABASE/database.db"
  [[ -f "\$db" ]] || return 0
  if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "[orphan-restore] sqlite3 is required to repair missing file owners" >&2
    exit 1
  fi
  local root_id
  root_id=\$(sqlite3 "\$db" "SELECT id FROM users WHERE username=char(114,111,111,116) ORDER BY id LIMIT 1;" 2>/dev/null || true)
  if [[ -z "\$root_id" ]]; then
    echo "[orphan-restore] refusing: staged database has no root user for missing-owner fallback" >&2
    exit 1
  fi
  local table
  for table in uploaded_files storage_files storage_folders storage_share_links cloud_file_refs encrypted_file_keys album_files albums album_share_links; do
    if [[ "\$(sqlite3 "\$db" "SELECT 1 FROM sqlite_master WHERE type=char(116,97,98,108,101) AND name='\$table' LIMIT 1;" 2>/dev/null || true)" != "1" ]]; then
      continue
    fi
    if ! sqlite3 "\$db" "PRAGMA table_info(\$table);" | awk -F'|' '\$2 == "owner_user_id" { found=1 } END { exit found ? 0 : 1 }'; then
      continue
    fi
    sqlite3 "\$db" "UPDATE \$table SET owner_user_id=\$root_id WHERE owner_user_id IS NOT NULL AND owner_user_id NOT IN (SELECT id FROM users);"
  done
  echo "[orphan-restore] reassigned file catalog rows whose owner no longer exists to root user id \$root_id"
}
repair_missing_file_catalog_owners
printf 'catalog_restore
status=started
backup_database=%s
backup_storage=%s
at=%s
' "\$BACKUP_DATABASE" "\$BACKUP_STORAGE" "\$(date -Is)" > "\$ACTION_LOCK"
chmod 600 "\$ACTION_LOCK" 2>/dev/null || true
echo "[orphan-restore] recovery action locked to catalog restore; plaintext export is now refused."
mkdir -p "\$BACKUP_STORAGE"
shopt -s dotglob nullglob
for item in "\$TARGET_STORAGE"/*; do
  [[ -e "\$item" ]] || continue
  [[ "\$(basename "\$item")" == ".reset_orphan_recovery" ]] && continue
  mv "\$item" "\$BACKUP_STORAGE/"
done
for item in "\$BUNDLE_STORAGE"/*; do
  [[ -e "\$item" ]] || continue
  mv "\$item" "\$TARGET_STORAGE/"
done
shopt -u dotglob nullglob
if [[ -e "\$TARGET_DATABASE" ]]; then
  mv "\$TARGET_DATABASE" "\$BACKUP_DATABASE"
  echo "[orphan-restore] moved current database to \$BACKUP_DATABASE"
fi
mv "\$STAGED_DATABASE" "\$TARGET_DATABASE"
printf 'status=completed\ncompleted_at=%s\n' "\$(date -Is)" >> "\$ACTION_LOCK"
echo "[orphan-restore] restored staged pre-reset database/catalog metadata to \$TARGET_DATABASE"
echo "[orphan-restore] restored orphaned storage contents to \$TARGET_STORAGE"
echo "[orphan-restore] moved post-reset storage contents to \$BACKUP_STORAGE"
echo "[orphan-restore] recovery action locked to catalog restore; plaintext export is now refused."
echo "[orphan-restore] restart the server, then verify cloud-drive catalog before deleting \$BACKUP_DATABASE and \$BACKUP_STORAGE"
EOF
  chmod 700 "$bundle_dir/restore_database_catalog_from_bundle.sh" 2>/dev/null || true
  cat > "$bundle_dir/README_SERVER_ENCRYPTED_RECOVERY.txt" <<EOF
This folder was created by test_for_develop.sh --reset before runtime DB/catalog state was cleared.

Purpose:
- storage/ is preserved by --reset, but existing pre-reset storage contents are moved into orphaned_storage/ so the post-reset storage root starts clean.
- This bundle keeps the pre-reset database/catalog metadata, encrypted files, and server-side keys needed to handle server_encrypted files. E2EE ciphertext and metadata are also preserved so they can be unlocked later with the user passphrase.
- If catalog restore finds file rows owned by users that no longer exist, those rows are reassigned to root.
- Choose exactly one recovery action. When either helper starts, recovery_action.lock prevents running the other path.

Important boundaries:
- server_encrypted export needs runtime_secrets/.filekey plus the pre-reset database metadata in database/.
- .fkey is not the server file encryption key; .filekey is used for server_encrypted files.
- Strict E2EE files cannot be decrypted with .filekey. They need the user's E2EE passphrase/key material; encrypted files and metadata remain in this bundle after plaintext export.
- Plaintext export is sensitive. Choose an output directory outside the live storage root.

Option 1: decrypt server_encrypted files to a plaintext folder:
$bundle_dir/export_server_encrypted_plaintext.sh /tmp/hackme_server_encrypted_plaintext_export

E2EE files are not decrypted by .filekey. After choosing plaintext export, keep this bundle. When the user passphrase is available, run the included script against the preserved ciphertext and metadata:
PYTHONPATH="$SOURCE_ROOT" "$PYTHON_BIN" "$bundle_dir/scripts/admin/decrypt_server_files.py" \\
  --db "$bundle_dir/database/database.db" \\
  --storage-root "$bundle_dir/orphaned_storage" \\
  --privacy-mode e2ee \
  --prompt-e2ee-passphrase \
  --output-dir /tmp/hackme_e2ee_plaintext_export \
  --confirm-plaintext-output

Option 2: import the pre-reset database/catalog metadata and orphaned encrypted storage files back after reset. Original file owners are preserved; if an owner user no longer exists, the row is reassigned to root:
$bundle_dir/restore_database_catalog_from_bundle.sh

The plaintext export helper uses this bundle's database/, orphaned_storage/, runtime_secrets/.filekey, and scripts/admin/decrypt_server_files.py. It locks out catalog restore before decryption starts.

The restore helper first stages this bundle's database/ without modifying the current runtime database, repairs missing file owners to root, locks out plaintext export, moves current post-reset storage contents into a backup folder inside this bundle, moves orphaned_storage/ back, and only then swaps the staged database into place after backing up the current runtime database/.
EOF
  RESET_ORPHAN_RECOVERY_BUNDLE="$bundle_dir"
  say "[dev-tmp] reset:    orphan recovery bundle=$bundle_dir"
}

prompt_reset_recovery_action() {
  local bundle_dir="$1"
  local choice output_dir
  [[ -n "$bundle_dir" && -d "$bundle_dir" ]] || return 0
  if [[ ! -t 0 || ! -t 1 ]]; then
    say "[dev-tmp] reset:    recovery action not selected in non-interactive mode"
    say "[dev-tmp] reset:    choose exactly one helper later:"
    say "[dev-tmp] reset:      1) $bundle_dir/export_server_encrypted_plaintext.sh <output-dir>"
    say "[dev-tmp] reset:      2) $bundle_dir/restore_database_catalog_from_bundle.sh"
    return 0
  fi
  say "[dev-tmp] reset recovery action (choose exactly one; the other will be locked out):"
  say "  1) decrypt server_encrypted files to a specified plaintext folder"
  say "  2) move encrypted files back to storage and restore DB/catalog metadata"
  while true; do
    printf 'Choose reset recovery action [1/2/skip]: '
    if ! read -r choice; then
      die "reset recovery action prompt was interrupted"
    fi
    case "${choice,,}" in
      1|decrypt|export|plaintext)
        prompt_value "Plaintext output directory" "/tmp/hackme_server_encrypted_plaintext_export" output_dir
        "$bundle_dir/export_server_encrypted_plaintext.sh" "$output_dir"
        return 0
        ;;
      2|restore|repair|db|catalog)
        "$bundle_dir/restore_database_catalog_from_bundle.sh"
        return 0
        ;;
      ""|skip|later|no)
        say "[dev-tmp] reset:    no recovery action selected now; use exactly one bundle helper later"
        return 0
        ;;
      *)
        say "Please choose 1, 2, or skip."
        ;;
    esac
  done
}

reset_runtime_state() {
  ensure_runtime_not_running "reset"
  say "[dev-tmp] reset:    runtime=$RUNTIME_ROOT"
  say "[dev-tmp] reset:    preserving storage/, venv/, and server-side secret/key files"
  say "[dev-tmp] reset:    warning: database/catalog rows are cleared; pre-reset storage files move to orphaned_storage/"
  say "[dev-tmp] reset:    creating storage/.reset_orphan_recovery bundle for server_encrypted export/import"
  mkdir -p "$RUNTIME_ROOT" "$EFFECTIVE_STORAGE_ROOT"
  write_reset_orphan_recovery_bundle
  local reset_recovery_bundle="$RESET_ORPHAN_RECOVERY_BUNDLE"
  rm -rf \
    "$RUNTIME_ROOT/database" \
    "$RUNTIME_ROOT/chats" \
    "$RUNTIME_ROOT/anchors" \
    "$RUNTIME_ROOT/reports" \
    "$RUNTIME_ROOT/logs" \
    "$RUNTIME_ROOT/pycache" \
    "$RUNTIME_ROOT/tmp" \
    "$RUNTIME_ROOT/temp" \
    "$RUNTIME_ROOT/dev_tokens.json" \
    "$RUNTIME_ROOT/server.pid"
  find "$RUNTIME_ROOT" -maxdepth 1 \( -name '*.sock' -o -name '*.lock' \) -type f -delete 2>/dev/null || true
  mkdir -p "$RUNTIME_ROOT/database" "$RUNTIME_ROOT/logs" "$RUNTIME_ROOT/chats" "$RUNTIME_ROOT/anchors" "$RUNTIME_ROOT/reports"
  say "[dev-tmp] reset:    completed"
  prompt_reset_recovery_action "$reset_recovery_bundle"
}

runtime_storage_inside_runtime() {
  [[ -n "${EFFECTIVE_STORAGE_ROOT:-}" && -e "$EFFECTIVE_STORAGE_ROOT" ]] || return 1
  path_is_inside_runtime "$EFFECTIVE_STORAGE_ROOT"
}

delete_runtime_root() {
  ensure_runtime_not_running "delete"
  if [[ ! -e "$RUNTIME_ROOT" ]]; then
    say "[dev-tmp] delete:   runtime does not exist: $RUNTIME_ROOT"
    return 0
  fi
  local stamp preserved_storage runtime_real
  runtime_real="$(readlink -f "$RUNTIME_ROOT" 2>/dev/null || true)"
  [[ -n "$runtime_real" && "$runtime_real" != "/" ]] || die "refusing to delete unsafe runtime root: $RUNTIME_ROOT"
  stamp="$(date +%Y%m%d_%H%M%S)"
  if runtime_storage_inside_runtime; then
    preserved_storage="$(dirname "$RUNTIME_ROOT")/$(basename "$RUNTIME_ROOT").storage-preserved-${stamp}"
    [[ ! -e "$preserved_storage" ]] || die "preserved storage path already exists: $preserved_storage"
    mv "$EFFECTIVE_STORAGE_ROOT" "$preserved_storage"
    say "[dev-tmp] delete:   moved storage to $preserved_storage"
  fi
  say "[dev-tmp] delete:   removing runtime=$RUNTIME_ROOT"
  rm -rf "$RUNTIME_ROOT"
  say "[dev-tmp] delete:   completed"
}

normalize_port() {
  local value="$1"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    die "port must be a number: $value"
  fi
  local number=$((10#$value))
  if (( number < 1 || number > 65535 )); then
    die "port must be between 1 and 65535: $value"
  fi
  NORMALIZED_PORT="$number"
}

port_is_available() {
  local candidate="$1"
  "$PYTHON_BIN" - "$HOST" "$candidate" <<'PY'
import socket
import sys

host = sys.argv[1]
port = int(sys.argv[2])

try:
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
except socket.gaierror:
    raise SystemExit(1)

if not addresses:
    raise SystemExit(1)

for family, socktype, proto, _canonname, sockaddr in addresses:
    try:
        with socket.socket(family, socktype, proto) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(sockaddr)
    except OSError:
        raise SystemExit(1)

raise SystemExit(0)
PY
}

port_pids() {
  local port="$1"
  local found=""
  if command -v lsof >/dev/null 2>&1; then
    found="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi
  fi
  if command -v ss >/dev/null 2>&1; then
    found="$(ss -ltnp "sport = :$port" 2>/dev/null \
      | grep -o 'pid=[0-9][0-9]*' \
      | sed 's/^pid=//' \
      | sort -u || true)"
    if [[ -n "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi
  fi
  if command -v python3 >/dev/null 2>&1; then
    found="$(python3 - "$port" <<'PY'
import os
import sys

try:
    port_hex = f"{int(sys.argv[1]):04X}"
except (IndexError, ValueError):
    raise SystemExit(0)

inodes = set()
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        with open(table, "r", encoding="ascii") as handle:
            lines = handle.readlines()[1:]
    except OSError:
        continue
    for line in lines:
        parts = line.split()
        if len(parts) < 10:
            continue
        local_address = parts[1]
        state = parts[3]
        inode = parts[9]
        if state == "0A" and local_address.rsplit(":", 1)[-1].upper() == port_hex:
            inodes.add(inode)

if not inodes:
    raise SystemExit(0)

pids = set()
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    fd_dir = os.path.join("/proc", pid, "fd")
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        continue
    for fd in fds:
        try:
            target = os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            continue
        if target.startswith("socket:[") and target[8:-1] in inodes:
            pids.add(pid)
            break

for pid in sorted(pids, key=int):
    print(pid)
PY
)"
    if [[ -n "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi
  fi
  if command -v pgrep >/dev/null 2>&1; then
    found="$(pgrep -f "gunicorn .*server:app .*--bind [^ ]*:${port}\\b" 2>/dev/null || true)"
    if [[ -n "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser -n tcp "$port" 2>/dev/null \
      | tr ' ' '\n' \
      | sed '/^$/d' \
      | sort -u
    return 0
  fi
  return 0
}

port_pid_list() {
  local port="$1"
  { port_pids "$port" || true; } | tr '\n' ' ' | sed 's/[[:space:]]*$//'
}

show_port_processes() {
  local port="$1"
  local pids="$2"
  if [[ -z "$pids" ]]; then
    say "[dev-tmp] port:      no listening process id could be identified for $port"
    return 0
  fi
  say "[dev-tmp] port:      listening pid(s): $pids"
  if command -v ps >/dev/null 2>&1; then
    ps -o pid,ppid,comm,args -p "$(printf '%s' "$pids" | tr ' ' ',')" 2>/dev/null || true
  fi
}

find_next_available_port() {
  local requested="$1"
  local candidate
  local upper=$((requested + 200))
  AVAILABLE_PORT=""
  if (( upper > 65535 )); then
    upper=65535
  fi

  for ((candidate = requested + 1; candidate <= upper; candidate++)); do
    if port_is_available "$candidate"; then
      AVAILABLE_PORT="$candidate"
      return 0
    fi
  done

  for ((candidate = 49152; candidate <= 65535; candidate++)); do
    if (( candidate >= requested && candidate <= upper )); then
      continue
    fi
    if port_is_available "$candidate"; then
      AVAILABLE_PORT="$candidate"
      return 0
    fi
  done

  return 1
}

use_next_available_port() {
  local requested="$1"
  if ! find_next_available_port "$requested"; then
    die "no available port found for host $HOST"
  fi
  PORT="$AVAILABLE_PORT"
  say "[dev-tmp] port:      $requested is occupied on $HOST; using $PORT"
}

kill_port_processes() {
  local requested="$1"
  local pids="$2"
  if [[ -z "$pids" ]]; then
    say "[dev-tmp] port:      cannot kill the process on port $requested because no pid was identified; falling back to another port"
    use_next_available_port "$requested"
    return 0
  fi
  say "[dev-tmp] port:      terminating pid(s): $pids"
  if ! kill $pids; then
    say "[dev-tmp] port:      failed to terminate pid(s): $pids; falling back to another port"
    use_next_available_port "$requested"
    return 0
  fi
  for _ in $(seq 1 20); do
    if port_is_available "$requested"; then
      PORT="$requested"
      say "[dev-tmp] port:      $requested is now available"
      return 0
    fi
    sleep 0.25
  done
  say "[dev-tmp] port:      port $requested is still occupied after terminating pid(s): $pids; falling back to another port"
  use_next_available_port "$requested"
}

is_dev_server_pid() {
  local pid="$1"
  local args=""
  local cwd=""
  [[ -n "$pid" && -r "/proc/$pid/cmdline" ]] || return 1
  args="$(tr '\000' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  [[ -n "$args" ]] || return 1
  cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  case "$args" in
    *"/hackme_web_dev_"*"/hackme_web/"*"server:app"*|*"/hackme_web_dev_"*"/hackme_web/"*"server.py"*)
      return 0
      ;;
    *"$SOURCE_ROOT/"*"server.py"*|*"$SOURCE_ROOT/"*"server:app"*)
      return 0
      ;;
    *"gunicorn server:app"*|*"gunicorn"*"server:app"*|*"server.py"*)
      case "$cwd" in
        "$SOURCE_ROOT"|"$SOURCE_ROOT"/*|/tmp/*/hackme_web|/tmp/*/hackme_web/*|/tmp/hackme_predeploy_capacity_*/profile_*/hackme_web|/tmp/hackme_predeploy_capacity_*/profile_*/hackme_web/*)
          return 0
          ;;
      esac
      ;;
  esac
  return 1
}

pid_pgid() {
  local pid="$1"
  ps -o pgid= -p "$pid" 2>/dev/null | tr -d '[:space:]' || true
}

append_unique_array_value() {
  local target_var="$1"
  local value="$2"
  local existing
  [[ -n "$value" ]] || return 0
  eval "local current=(\"\${${target_var}[@]:-}\")"
  for existing in "${current[@]:-}"; do
    [[ "$existing" == "$value" ]] && return 0
  done
  eval "$target_var+=(\"\$value\")"
}

collect_descendant_pids() {
  local parent="$1"
  local child
  if ! command -v pgrep >/dev/null 2>&1; then
    return 0
  fi
  while IFS= read -r child; do
    [[ -n "$child" ]] || continue
    printf '%s\n' "$child"
    collect_descendant_pids "$child"
  done < <(pgrep -P "$parent" 2>/dev/null || true)
}

group_has_live_processes() {
  local pgid="$1"
  [[ -n "$pgid" ]] || return 1
  kill -0 "-$pgid" 2>/dev/null
}

shutdown_dev_server_pids() {
  local pids="$1"
  local targets=()
  local groups=()
  local pid child pgid own_pgid
  own_pgid="$(pid_pgid $$)"

  for pid in $pids; do
    if ! is_dev_server_pid "$pid"; then
      say "[dev-tmp] shutdown: skip non-dev pid $pid"
      continue
    fi
    append_unique_array_value targets "$pid"
    while IFS= read -r child; do
      append_unique_array_value targets "$child"
    done < <(collect_descendant_pids "$pid")
    pgid="$(pid_pgid "$pid")"
    if [[ -n "$pgid" && "$pgid" != "$own_pgid" ]]; then
      append_unique_array_value groups "$pgid"
    fi
  done

  if [[ "${#targets[@]}" == "0" && "${#groups[@]}" == "0" ]]; then
    say "[dev-tmp] shutdown: no matching hackme_web dev server process found"
    return 0
  fi

  if [[ "${#groups[@]}" != "0" ]]; then
    say "[dev-tmp] shutdown: terminating process group(s): ${groups[*]}"
    for pgid in "${groups[@]}"; do
      kill -TERM "-$pgid" 2>/dev/null || true
    done
  fi
  if [[ "${#targets[@]}" != "0" ]]; then
    say "[dev-tmp] shutdown: terminating pid tree: ${targets[*]}"
    kill -TERM "${targets[@]}" 2>/dev/null || true
  fi

  for _ in $(seq 1 40); do
    local alive=()
    for pgid in "${groups[@]:-}"; do
      if group_has_live_processes "$pgid"; then
        alive+=("group:$pgid")
      fi
    done
    for pid in "${targets[@]:-}"; do
      if kill -0 "$pid" 2>/dev/null; then
        alive+=("pid:$pid")
      fi
    done
    if [[ "${#alive[@]}" == "0" ]]; then
      say "[dev-tmp] shutdown: stopped"
      return 0
    fi
    sleep 0.25
  done

  if [[ "${#groups[@]}" != "0" ]]; then
    say "[dev-tmp] shutdown: forcing process group(s): ${groups[*]}"
    for pgid in "${groups[@]}"; do
      kill -KILL "-$pgid" 2>/dev/null || true
    done
  fi
  if [[ "${#targets[@]}" != "0" ]]; then
    say "[dev-tmp] shutdown: forcing pid tree: ${targets[*]}"
    kill -KILL "${targets[@]}" 2>/dev/null || true
  fi
}

collect_pid_file_pids() {
  local candidates=()
  local file pid
  append_unique_array_value candidates "$SOURCE_ROOT/runtime/server.pid"
  if [[ -n "${CUSTOM_RUNTIME_ROOT:-}" ]]; then
    append_unique_array_value candidates "$CUSTOM_RUNTIME_ROOT/server.pid"
  fi
  while IFS= read -r file; do
    append_unique_array_value candidates "$file"
  done < <(find /tmp -maxdepth 5 \
    \( -path '/tmp/hackme_web_dev_*/runtime/server.pid' \
       -o -path '/tmp/hackme_web_dev_*/hackme_web/runtime/server.pid' \
       -o -path '/tmp/*/hackme_web/runtime/server.pid' \
       -o -path '/tmp/hackme_predeploy_capacity_*/profile_*/hackme_web/runtime/server.pid' \) \
    -type f 2>/dev/null || true)
  for file in "${candidates[@]:-}"; do
    [[ -r "$file" ]] || continue
    pid="$(sed -n '1p' "$file" 2>/dev/null | tr -dc '0-9')"
    [[ -n "$pid" ]] || continue
    printf '%s\n' "$pid"
  done
}

scan_dev_server_pids() {
  command -v pgrep >/dev/null 2>&1 || return 0
  {
    pgrep -f 'gunicorn .*server:app' 2>/dev/null || true
    pgrep -f 'server.py' 2>/dev/null || true
  } | sort -u
}

pid_matches_shutdown_port() {
  local pid="$1"
  local port="$2"
  local args env_port
  [[ -n "$pid" && -r "/proc/$pid/cmdline" ]] || return 1
  args="$(tr '\000' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  if [[ "$args" == *"--bind"*":$port"* || "$args" == *"--bind="*":$port"* ]]; then
    return 0
  fi
  if [[ -r "/proc/$pid/environ" ]]; then
    env_port="$(tr '\000' '\n' < "/proc/$pid/environ" 2>/dev/null | sed -n 's/^PORT=//p' | tail -n 1)"
    if [[ "$env_port" == "$port" ]]; then
      return 0
    fi
  fi
  return 1
}

shutdown_candidate_pids_for_port() {
  local port="$1"
  local pid
  port_pid_list "$port" | tr ' ' '\n'
  while IFS= read -r pid; do
    [[ -n "$pid" ]] || continue
    pid_matches_shutdown_port "$pid" "$port" || continue
    printf '%s\n' "$pid"
  done < <({ collect_pid_file_pids; scan_dev_server_pids; } | sort -u)
}

shutdown_dev_servers_for_port() {
  normalize_port "$PORT"
  PORT="$NORMALIZED_PORT"
  local listener_pids pids
  listener_pids="$(port_pid_list "$PORT")"
  if [[ -z "$listener_pids" ]]; then
    say "[dev-tmp] shutdown: no listener on $HOST:$PORT; checking dev pid files and process scan"
  else
    show_port_processes "$PORT" "$listener_pids"
  fi
  pids="$(shutdown_candidate_pids_for_port "$PORT" | paste -sd ' ' - | sed 's/[[:space:]]*$//')"
  shutdown_dev_server_pids "$pids"
}

resolve_occupied_port_interactively() {
  local requested="$1"
  local pids
  local choice

  pids="$(port_pid_list "$requested")"
  say "[dev-tmp] port:      $requested is occupied on $HOST"
  show_port_processes "$requested" "$pids"

  while true; do
    printf '[dev-tmp] choose: [k]ill process, use [p]ort fallback, [q]uit (default: p): '
    if ! read -r choice; then
      choice="p"
    fi
    case "$choice" in
      k|K|kill|Kill)
        kill_port_processes "$requested" "$pids"
        return 0
        ;;
      ""|p|P|port|Port)
        use_next_available_port "$requested"
        return 0
        ;;
      q|Q|quit|Quit)
        die "port $requested is occupied"
        ;;
      *)
        say "[dev-tmp] choose k, p, or q"
        ;;
    esac
  done
}

resolve_server_port() {
  normalize_port "$PORT"
  local requested="$NORMALIZED_PORT"
  PORT="$requested"

  if port_is_available "$requested"; then
    return 0
  fi

  case "$PORT_CONFLICT_ACTION" in
    ask)
      if [[ -t 0 && -t 1 ]]; then
        resolve_occupied_port_interactively "$requested"
      else
        die "port $requested is occupied and --port-conflict ask requires a TTY"
      fi
      ;;
    kill)
      local pids
      pids="$(port_pid_list "$requested")"
      show_port_processes "$requested" "$pids"
      kill_port_processes "$requested" "$pids"
      ;;
    fallback)
      use_next_available_port "$requested"
      ;;
    fail)
      die "port $requested is occupied on $HOST"
      ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --cli|-cli)
      CLI_MODE=1
      shift
      ;;
    --host)
      HOST="${2:?missing host}"
      shift 2
      ;;
    --port)
      PORT="${2:?missing port}"
      shift 2
      ;;
    --trusted-hosts)
      TRUSTED_HOSTS="${2:?missing trusted hosts list}"
      shift 2
      ;;
    --public-host|--public-ip)
      PUBLIC_HOST="${2:?missing public host}"
      shift 2
      ;;
    --allow-any-host|--disable-trusted-hosts)
      DISABLE_TRUSTED_HOSTS=1
      shift
      ;;
    --enforce-trusted-hosts)
      DISABLE_TRUSTED_HOSTS=0
      shift
      ;;
    --shutdown|--stop)
      SHUTDOWN=1
      shift
      ;;
    --feature-mode)
      FEATURE_MODE="${2:?missing feature mode}"
      FEATURE_MODE_SET=1
      shift 2
      ;;
    --feature-bundles|--feature-packages|--feature-presets)
      FEATURE_BUNDLES="${2:?missing feature bundle list}"
      FEATURE_MODE="bundles"
      FEATURE_MODE_SET=1
      shift 2
      ;;
    --features|--enable-features)
      FEATURE_LIST="${2:?missing feature list}"
      if [[ "$FEATURE_MODE_SET" == "0" ]]; then
        FEATURE_MODE="custom"
      fi
      shift 2
      ;;
    --token-features|--internal-test-token-features)
      DEV_TOKEN_FEATURES="${2:?missing generated token feature list}"
      shift 2
      ;;
    --token-ttl-minutes|--internal-test-token-ttl-minutes)
      DEV_TOKEN_TTL_MINUTES="${2:?missing token ttl minutes}"
      shift 2
      ;;
    --token-user)
      DEV_TOKEN_USER="${2:?missing token username}"
      shift 2
      ;;
    --token-password)
      DEV_TOKEN_PASSWORD="${2:?missing token password}"
      shift 2
      ;;
    --token-role)
      DEV_TOKEN_ROLE="${2:?missing token role}"
      shift 2
      ;;
    --security)
      SECURITY_SETTINGS_ENABLED="${2:?missing security value}"
      shift 2
      ;;
    --security-enabled|--enable-security)
      SECURITY_SETTINGS_ENABLED=1
      shift
      ;;
    --no-security|--disable-security)
      SECURITY_SETTINGS_ENABLED=0
      shift
      ;;
    --session-idle-timeout-minutes|--idle-timeout-minutes|--logout-countdown-minutes)
      SESSION_IDLE_TIMEOUT_MINUTES="${2:?missing session idle timeout minutes}"
      shift 2
      ;;
    --server-mode)
      SERVER_MODE="${2:?missing server mode}"
      shift 2
      ;;
    --add-account)
      append_csv_value EXTRA_ACCOUNTS "${2:?missing account spec}"
      shift 2
      ;;
    --accounts)
      EXTRA_ACCOUNTS="${2:?missing account list}"
      shift 2
      ;;
    --port-conflict)
      PORT_CONFLICT_ACTION="${2:?missing port conflict action}"
      shift 2
      ;;
    --btc-trade-autostart)
      BTC_TRADE_AUTOSTART=1
      shift
      ;;
    --no-btc-trade-autostart)
      BTC_TRADE_AUTOSTART=0
      shift
      ;;
    --backtest-probe-on-startup)
      BACKTEST_PROBE_ON_STARTUP=1
      shift
      ;;
    --trading-background-dev-ready|--enable-trading-background-dev-ready)
      TRADING_BACKGROUND_DEV_READY=1
      shift
      ;;
    --no-trading-background-dev-ready|--disable-trading-background-dev-ready)
      TRADING_BACKGROUND_DEV_READY=0
      shift
      ;;
    --server-runner)
      SERVER_RUNNER="${2:?missing server runner}"
      shift 2
      ;;
    --gunicorn-workers)
      GUNICORN_WORKERS="${2:?missing gunicorn worker count}"
      shift 2
      ;;
    --gunicorn-threads)
      GUNICORN_THREADS="${2:?missing gunicorn thread count}"
      shift 2
      ;;
    --gunicorn-timeout)
      GUNICORN_TIMEOUT="${2:?missing gunicorn timeout}"
      shift 2
      ;;
    --gunicorn-graceful-timeout)
      GUNICORN_GRACEFUL_TIMEOUT="${2:?missing gunicorn graceful timeout}"
      shift 2
      ;;
    --gunicorn-keep-alive)
      GUNICORN_KEEP_ALIVE="${2:?missing gunicorn keep-alive}"
      shift 2
      ;;
    --gunicorn-backlog)
      GUNICORN_BACKLOG="${2:?missing gunicorn backlog}"
      shift 2
      ;;
    --gunicorn-max-requests)
      GUNICORN_MAX_REQUESTS="${2:?missing gunicorn max requests}"
      shift 2
      ;;
    --gunicorn-max-requests-jitter)
      GUNICORN_MAX_REQUESTS_JITTER="${2:?missing gunicorn max requests jitter}"
      shift 2
      ;;
    --capacity-probe|--retest-capacity|--refresh-capacity)
      CAPACITY_PROBE_MODE=force
      shift
      ;;
    --capacity-probe-light|--light-capacity-probe)
      CAPACITY_PROBE_MODE=force
      CAPACITY_PROBE_TIER=legacy
      shift
      ;;
    --capacity-probe-tier|--capacity-tier)
      CAPACITY_PROBE_TIER="${2:?missing capacity probe tier}"
      shift 2
      ;;
    --no-capacity-probe)
      CAPACITY_PROBE_MODE=never
      shift
      ;;
    --hls-slot-probe|--hls-capacity-probe)
      HLS_SLOT_PROBE_MODE=force
      shift
      ;;
    --no-hls-slot-probe)
      HLS_SLOT_PROBE_MODE=never
      shift
      ;;
    --bt-backend|--bt-download-backend)
      BT_DOWNLOAD_BACKEND="${2:?missing BT backend}"
      BT_DOWNLOAD_CONFIG_SET=1
      shift 2
      ;;
    --transmission-rpc-url)
      TRANSMISSION_RPC_URL="${2:?missing Transmission RPC URL}"
      TRANSMISSION_CONFIG_SET=1
      shift 2
      ;;
    --transmission-rpc-username)
      TRANSMISSION_RPC_USERNAME="${2:?missing Transmission RPC username}"
      TRANSMISSION_CONFIG_SET=1
      shift 2
      ;;
    --transmission-rpc-password)
      TRANSMISSION_RPC_PASSWORD="${2:?missing Transmission RPC password}"
      TRANSMISSION_CONFIG_SET=1
      shift 2
      ;;
    --setup-transmission-backend|--configure-transmission-backend)
      SETUP_TRANSMISSION_BACKEND=1
      TRANSMISSION_CONFIG_SET=1
      BT_DOWNLOAD_CONFIG_SET=1
      shift
      ;;
    --no-setup-transmission-backend)
      SETUP_TRANSMISSION_BACKEND=0
      TRANSMISSION_CONFIG_SET=1
      shift
      ;;
    --transmission-setup-script)
      TRANSMISSION_SETUP_SCRIPT="${2:?missing Transmission setup helper path}"
      TRANSMISSION_CONFIG_SET=1
      shift 2
      ;;
    --transmission-settings-file)
      TRANSMISSION_SETUP_SETTINGS_FILE="${2:?missing Transmission settings file}"
      TRANSMISSION_CONFIG_SET=1
      shift 2
      ;;
    --transmission-service)
      TRANSMISSION_SETUP_SERVICE="${2:?missing Transmission service name}"
      TRANSMISSION_CONFIG_SET=1
      shift 2
      ;;
    --transmission-rpc-bind-address)
      TRANSMISSION_SETUP_RPC_BIND_ADDRESS="${2:?missing Transmission RPC bind address}"
      TRANSMISSION_CONFIG_SET=1
      shift 2
      ;;
    --transmission-rpc-whitelist)
      TRANSMISSION_SETUP_RPC_WHITELIST="${2:?missing Transmission RPC whitelist}"
      TRANSMISSION_CONFIG_SET=1
      shift 2
      ;;
    --transmission-rpc-whitelist-enabled)
      TRANSMISSION_SETUP_RPC_WHITELIST_ENABLED="${2:?missing Transmission RPC whitelist enabled value}"
      TRANSMISSION_CONFIG_SET=1
      shift 2
      ;;
    --transmission-rpc-authentication-required)
      TRANSMISSION_SETUP_RPC_AUTHENTICATION_REQUIRED="${2:?missing Transmission RPC authentication required value}"
      TRANSMISSION_CONFIG_SET=1
      shift 2
      ;;
    --transmission-disable-rpc-auth|--disable-transmission-rpc-auth|--transmission-no-rpc-auth)
      TRANSMISSION_SETUP_RPC_AUTHENTICATION_REQUIRED=false
      TRANSMISSION_CONFIG_SET=1
      shift
      ;;
    --transmission-allow-any-rpc-ip|--allow-any-transmission-rpc-ip)
      TRANSMISSION_SETUP_ALLOW_ANY_RPC_IP=1
      TRANSMISSION_CONFIG_SET=1
      shift
      ;;
    --bt-download-staging-dir|--transmission-download-dir|--transmission-staging-dir)
      BT_DOWNLOAD_STAGING_DIR="${2:?missing BT download staging directory}"
      TRANSMISSION_CONFIG_SET=1
      export HACKME_BT_DOWNLOAD_STAGING_DIR="$BT_DOWNLOAD_STAGING_DIR"
      shift 2
      ;;
    --remote-download-global|--remote-download-max-concurrent-global)
      HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL="${2:?missing remote download global concurrency}"
      REMOTE_DOWNLOAD_LIMITS_SET=1
      export HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_GLOBAL
      HACKME_REMOTE_DOWNLOAD_LIMITS_PREFER_ENV=1
      export HACKME_REMOTE_DOWNLOAD_LIMITS_PREFER_ENV
      shift 2
      ;;
    --remote-download-per-user|--remote-download-max-concurrent-per-user)
      HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER="${2:?missing remote download per-user concurrency}"
      REMOTE_DOWNLOAD_LIMITS_SET=1
      export HACKME_REMOTE_DOWNLOAD_MAX_CONCURRENT_PER_USER
      HACKME_REMOTE_DOWNLOAD_LIMITS_PREFER_ENV=1
      export HACKME_REMOTE_DOWNLOAD_LIMITS_PREFER_ENV
      shift 2
      ;;
    --capacity-defaults-file)
      CAPACITY_DEFAULTS_FILE="${2:?missing capacity defaults file}"
      shift 2
      ;;
    --capacity-report-file)
      CAPACITY_REPORT_DEFAULTS_FILE="${2:?missing capacity report file}"
      shift 2
      ;;
    --cloud-drive-root|--cloud-drive-storage-root)
      CLOUD_DRIVE_STORAGE_ROOT="${2:?missing cloud drive storage root}"
      shift 2
      ;;
    --cloud-drive-max-mb|--cloud-drive-global-capacity-limit-mb)
      CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB="${2:?missing cloud drive capacity limit MB}"
      shift 2
      ;;
    --cloud-drive-max-size|--cloud-drive-capacity|--cloud-drive-max-usage)
      CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB="${2:?missing cloud drive max size}"
      shift 2
      ;;
    --max-content-mb|--upload-request-max-mb|--html-learning-max-content-mb)
      MAX_CONTENT_MB="${2:?missing max content MB}"
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --backup)
      BACKUP_RUNTIME=1
      if [[ $# -ge 2 && "${2:-}" != --* ]]; then
        BACKUP_ARCHIVE="$2"
        shift 2
      else
        shift
      fi
      ;;
    --backup-file|--backup-archive)
      BACKUP_RUNTIME=1
      BACKUP_ARCHIVE="${2:?missing backup archive path}"
      shift 2
      ;;
    --restore)
      RESTORE_ARCHIVE="${2:?missing restore archive path}"
      shift 2
      ;;
    --reset)
      RESET_RUNTIME=1
      shift
      ;;
    --delete)
      DELETE_RUNTIME=1
      shift
      ;;
    --run-root)
      RUN_ROOT="${2:?missing run root}"
      RUN_ROOT_SET=1
      shift 2
      ;;
    --runtime-root|--runtime-dir|--runtime-directory)
      CUSTOM_RUNTIME_ROOT="${2:?missing runtime root}"
      RUNTIME_ROOT_SET=1
      shift 2
      ;;
    --in-place|--no-copy)
      IN_PLACE=1
      RUNTIME_LAYOUT_SET=1
      shift
      ;;
    --runtime-in-source|--source-runtime|--deploy-in-place)
      IN_PLACE=1
      RUNTIME_IN_SOURCE=1
      RUNTIME_LAYOUT_SET=1
      shift
      ;;
    --tmp-runtime)
      RUNTIME_IN_SOURCE=0
      RUNTIME_LAYOUT_SET=1
      shift
      ;;
    --copy)
      IN_PLACE=0
      RUNTIME_IN_SOURCE=0
      RUNTIME_LAYOUT_SET=1
      shift
      ;;
    --skip-install)
      SKIP_INSTALL=1
      shift
      ;;
    --requirements-file)
      REQUIREMENTS_FILE="${2:?missing requirements file}"
      REQUIREMENTS_FILE_SET=1
      shift 2
      ;;
    --foreground)
      FOREGROUND=1
      shift
      ;;
    --root-password)
      ROOT_PASSWORD="${2:?missing root password}"
      shift 2
      ;;
    --manager-password)
      MANAGER_PASSWORD="${2:?missing manager password}"
      shift 2
      ;;
    --test-password)
      TEST_PASSWORD="${2:?missing test password}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown argument: $1"
      ;;
  esac
done

if [[ -n "$FEATURE_LIST" && -z "${HACKME_DEV_FEATURE_MODE:-}" && "$FEATURE_MODE_SET" == "0" ]]; then
  FEATURE_MODE="custom"
fi
if [[ -n "$FEATURE_BUNDLES" && -z "${HACKME_DEV_FEATURE_MODE:-}" && "$FEATURE_MODE_SET" == "0" ]]; then
  FEATURE_MODE="bundles"
fi

normalize_capacity_probe_mode
normalize_capacity_probe_tier
normalize_hls_slot_probe_mode
normalize_bt_download_backend
export HACKME_BT_BACKEND="$BT_DOWNLOAD_BACKEND"
export HACKME_TRANSMISSION_RPC_URL="$TRANSMISSION_RPC_URL"
export HACKME_TRANSMISSION_RPC_USERNAME="$TRANSMISSION_RPC_USERNAME"
export HACKME_TRANSMISSION_RPC_PASSWORD="$TRANSMISSION_RPC_PASSWORD"
export HACKME_BT_DOWNLOAD_STAGING_DIR="$BT_DOWNLOAD_STAGING_DIR"
if [[ "$CLI_MODE" == "1" || "$SHUTDOWN" == "1" || "$BACKUP_RUNTIME" == "1" || -n "$RESTORE_ARCHIVE" || "$RESET_RUNTIME" == "1" || "$DELETE_RUNTIME" == "1" ]]; then
  load_local_capacity_report_defaults || load_local_capacity_defaults
fi

if [[ "$SHUTDOWN" == "1" ]]; then
  normalize_port_conflict_action
  shutdown_dev_servers_for_port
  exit 0
fi

if [[ "$CLI_MODE" != "1" ]] && ! runtime_maintenance_action_requested; then
  prompt_runtime_config
fi
if runtime_maintenance_action_requested; then
  normalize_runtime_maintenance_options
else
  normalize_runtime_options
  normalize_yes_no_value "$DISABLE_TRUSTED_HOSTS" "disable trusted hosts"
  DISABLE_TRUSTED_HOSTS="$NORMALIZED_YES_NO"
  finalize_trusted_hosts
fi

RUN_ROOT="${RUN_ROOT:-/tmp/hackme_web_dev_${RUN_ID}_$$}"
if runtime_maintenance_action_requested; then
  ensure_single_runtime_maintenance_action
fi
if [[ "$DRY_RUN" == "1" ]]; then
  print_resolved_config
  exit 0
fi

if [[ "$IN_PLACE" == "1" ]]; then
  COPY_ROOT="$SOURCE_ROOT"
  if [[ "$RUNTIME_IN_SOURCE" == "1" ]]; then
    RUNTIME_ROOT="$SOURCE_ROOT/runtime"
  else
    RUNTIME_ROOT="$RUN_ROOT/runtime"
  fi
else
  COPY_ROOT="$RUN_ROOT/hackme_web"
  RUNTIME_ROOT="$COPY_ROOT/runtime"
fi
if [[ -n "$CUSTOM_RUNTIME_ROOT" ]]; then
  RUNTIME_ROOT="$CUSTOM_RUNTIME_ROOT"
fi
EFFECTIVE_STORAGE_ROOT="${CLOUD_DRIVE_STORAGE_ROOT:-$RUNTIME_ROOT/storage}"
LOG_CAPTURE="$RUNTIME_ROOT/logs/server_direct.out"
GUNICORN_ACCESS_LOG="$RUNTIME_ROOT/logs/gunicorn_access.log"
GUNICORN_ERROR_LOG="$RUNTIME_ROOT/logs/gunicorn_error.log"
PID_FILE="$RUNTIME_ROOT/server.pid"

if [[ "$BACKUP_RUNTIME" == "1" || -n "$RESTORE_ARCHIVE" || "$RESET_RUNTIME" == "1" || "$DELETE_RUNTIME" == "1" ]]; then
  ensure_single_runtime_maintenance_action
  if [[ "$BACKUP_RUNTIME" == "1" ]]; then
    backup_runtime_state "$BACKUP_ARCHIVE"
  elif [[ -n "$RESTORE_ARCHIVE" ]]; then
    restore_runtime_state "$RESTORE_ARCHIVE"
  elif [[ "$RESET_RUNTIME" == "1" ]]; then
    reset_runtime_state
  else
    delete_runtime_root
  fi
  exit 0
fi

if [[ "$RUNTIME_IN_SOURCE" == "1" ]]; then
  :
elif [[ "$IN_PLACE" == "1" ]]; then
  mkdir -p "$RUN_ROOT"
else
  [[ ! -e "$COPY_ROOT" ]] || die "tmp copy already exists: $COPY_ROOT"
  copy_repo
fi
ensure_official_workflows_source "$COPY_ROOT"
mkdir -p \
  "$RUNTIME_ROOT/database" \
  "$RUNTIME_ROOT/logs" \
  "$RUNTIME_ROOT/chats" \
  "$RUNTIME_ROOT/anchors" \
  "$EFFECTIVE_STORAGE_ROOT" \
  "$RUNTIME_ROOT/reports"
touch "$LOG_CAPTURE" "$GUNICORN_ACCESS_LOG" "$GUNICORN_ERROR_LOG"

resolve_python
migrate_legacy_runtime_storage_to_cloud_drive_root
run_transmission_backend_setup_if_requested
if [[ "$PYTHON_BIN" != "python3" ]]; then
  say "[dev-tmp] python:    $PYTHON_BIN"
else
  say "[dev-tmp] python:    python3 (reuse current environment)"
fi
resolve_server_port
finalize_trusted_hosts
if [[ "$ROOT_PASSWORD" == "root" && "$MANAGER_PASSWORD" == "admin" && "$TEST_PASSWORD" == "test" ]]; then
  DEFAULT_ACCOUNT_PASSWORDS=1
else
  DEFAULT_ACCOUNT_PASSWORDS=0
fi

export HACKME_RUNTIME_DIR="$RUNTIME_ROOT"
export HACKME_DEV_RUNTIME_ROOT="$CUSTOM_RUNTIME_ROOT"
export HTML_LEARNING_DB_DIR="$RUNTIME_ROOT/database"
export HTML_LEARNING_LOG_DIR="$RUNTIME_ROOT/logs"
export HTML_LEARNING_CHAT_DIR="$RUNTIME_ROOT/chats"
export HTML_LEARNING_ANCHOR_DIR="$RUNTIME_ROOT/anchors"
export HTML_LEARNING_STORAGE_DIR="$EFFECTIVE_STORAGE_ROOT"
export HTML_LEARNING_REPORTS_DIR="$RUNTIME_ROOT/reports"
export HACKME_BT_DOWNLOAD_STAGING_DIR="$BT_DOWNLOAD_STAGING_DIR"
export HTML_LEARNING_HOST="$HOST"
export HTML_LEARNING_PORT="$PORT"
if [[ "$DISABLE_TRUSTED_HOSTS" == "1" ]]; then
  export HTML_LEARNING_DISABLE_TRUSTED_HOSTS=1
elif [[ -n "$TRUSTED_HOSTS" ]]; then
  export HTML_LEARNING_TRUSTED_HOSTS="$TRUSTED_HOSTS"
fi
export HTML_LEARNING_ROOT_PASSWORD="$ROOT_PASSWORD"
export HTML_LEARNING_MANAGER_PASSWORD="$MANAGER_PASSWORD"
export HTML_LEARNING_TEST_PASSWORD="$TEST_PASSWORD"
if [[ -n "$MAX_CONTENT_MB" ]]; then
  export HTML_LEARNING_MAX_CONTENT_MB="$MAX_CONTENT_MB"
  export HACKME_DEV_MAX_CONTENT_MB="$MAX_CONTENT_MB"
fi
export HTML_LEARNING_ARGON2_TIME_COST="${HTML_LEARNING_ARGON2_TIME_COST:-1}"
export HTML_LEARNING_ARGON2_MEMORY_COST="${HTML_LEARNING_ARGON2_MEMORY_COST:-8192}"
export HTML_LEARNING_ARGON2_PARALLELISM="${HTML_LEARNING_ARGON2_PARALLELISM:-1}"
export HACKME_DEV_FEATURE_MODE="$FEATURE_MODE"
export HACKME_DEV_FEATURES="$FEATURE_LIST"
export HACKME_DEV_FEATURE_BUNDLES="$FEATURE_BUNDLES"
export HACKME_DEV_IN_PLACE="$IN_PLACE"
export HACKME_DEV_RUNTIME_IN_SOURCE="$RUNTIME_IN_SOURCE"
export HACKME_DEV_TOKEN_FEATURES="$DEV_TOKEN_FEATURES"
export HACKME_DEV_TOKEN_TTL_MINUTES="$DEV_TOKEN_TTL_MINUTES"
export HACKME_DEV_TOKEN_USER="$DEV_TOKEN_USER"
export HACKME_DEV_TOKEN_PASSWORD="$DEV_TOKEN_PASSWORD"
export HACKME_DEV_TOKEN_ROLE="$DEV_TOKEN_ROLE"
export HACKME_DEV_INTERNAL_TEST_TOKEN_FEATURES="$DEV_TOKEN_FEATURES"
export HACKME_DEV_TOKENS_FILE="$RUNTIME_ROOT/dev_tokens.json"
export HACKME_DEV_SECURITY_ENABLED="$SECURITY_SETTINGS_ENABLED"
export HACKME_DEV_SESSION_IDLE_TIMEOUT_MINUTES="$SESSION_IDLE_TIMEOUT_MINUTES"
export HACKME_DEV_SERVER_MODE="$SERVER_MODE"
export HACKME_DEV_EXTRA_ACCOUNTS="$EXTRA_ACCOUNTS"
export HACKME_DEV_BTC_TRADE_AUTOSTART="$BTC_TRADE_AUTOSTART"
export HACKME_DEV_BACKTEST_PROBE_ON_STARTUP="$BACKTEST_PROBE_ON_STARTUP"
export HTML_LEARNING_TRADING_BACKTEST_PROBE_ON_STARTUP="$BACKTEST_PROBE_ON_STARTUP"
export HACKME_DEV_TRADING_BACKGROUND_DEV_READY="$TRADING_BACKGROUND_DEV_READY"
export HACKME_DEV_DEFAULT_ACCOUNT_PASSWORDS="$DEFAULT_ACCOUNT_PASSWORDS"
export HACKME_DEV_SERVER_RUNNER="$SERVER_RUNNER"
export HACKME_DEV_GUNICORN_WORKERS="$GUNICORN_WORKERS"
export HACKME_DEV_GUNICORN_THREADS="$GUNICORN_THREADS"
export HACKME_DEV_GUNICORN_TIMEOUT="$GUNICORN_TIMEOUT"
export HACKME_DEV_GUNICORN_MAX_REQUESTS="$GUNICORN_MAX_REQUESTS"
export HACKME_DEV_GUNICORN_MAX_REQUESTS_JITTER="$GUNICORN_MAX_REQUESTS_JITTER"
export HACKME_DEV_CAPACITY_PROBE="$CAPACITY_PROBE_MODE"
export HACKME_DEV_CAPACITY_PROBE_TIER="$CAPACITY_PROBE_TIER"
export HACKME_DEV_CAPACITY_DEFAULTS_FILE="$CAPACITY_DEFAULTS_FILE"
export HACKME_DEV_CLOUD_DRIVE_STORAGE_ROOT="$CLOUD_DRIVE_STORAGE_ROOT"
export HACKME_DEV_CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB="$CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB"
export HACKME_DEV_SETUP_TRANSMISSION_BACKEND="$SETUP_TRANSMISSION_BACKEND"
export HACKME_DEV_TRANSMISSION_SETUP_SCRIPT="$TRANSMISSION_SETUP_SCRIPT"
export HACKME_DEV_TRANSMISSION_SERVICE="$TRANSMISSION_SETUP_SERVICE"
export HACKME_DEV_TRANSMISSION_SETTINGS_FILE="$TRANSMISSION_SETUP_SETTINGS_FILE"
export HACKME_DEV_TRANSMISSION_RPC_BIND_ADDRESS="$TRANSMISSION_SETUP_RPC_BIND_ADDRESS"
export HACKME_DEV_TRANSMISSION_RPC_WHITELIST="$TRANSMISSION_SETUP_RPC_WHITELIST"
export HACKME_DEV_TRANSMISSION_RPC_WHITELIST_ENABLED="$TRANSMISSION_SETUP_RPC_WHITELIST_ENABLED"
export HACKME_DEV_TRANSMISSION_RPC_AUTHENTICATION_REQUIRED="$TRANSMISSION_SETUP_RPC_AUTHENTICATION_REQUIRED"
export HACKME_DEV_TRANSMISSION_ALLOW_ANY_RPC_IP="$TRANSMISSION_SETUP_ALLOW_ANY_RPC_IP"
if [[ "$SERVER_RUNNER" == "flask" ]]; then
  export HACKME_ALLOW_DIRECT_SERVER=1
fi
export HTML_LEARNING_BACKPRESSURE_ENABLED="${HTML_LEARNING_BACKPRESSURE_ENABLED:-1}"
if [[ "$SERVER_RUNNER" == "gunicorn" ]]; then
  export HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY="${HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY:-$GUNICORN_THREADS}"
else
  export HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY="${HTML_LEARNING_BACKPRESSURE_THREAD_CAPACITY:-auto}"
fi
export HTML_LEARNING_BACKPRESSURE_NORMAL_LIMIT="${HTML_LEARNING_BACKPRESSURE_NORMAL_LIMIT:-auto}"
export HTML_LEARNING_BACKPRESSURE_HEAVY_LIMIT="${HTML_LEARNING_BACKPRESSURE_HEAVY_LIMIT:-auto}"
export HTML_LEARNING_BACKPRESSURE_FAST_LANE_RESERVED="${HTML_LEARNING_BACKPRESSURE_FAST_LANE_RESERVED:-auto}"
export HTML_LEARNING_BACKPRESSURE_RETRY_AFTER_SECONDS="${HTML_LEARNING_BACKPRESSURE_RETRY_AFTER_SECONDS:-2}"
if [[ "$SECURITY_SETTINGS_ENABLED" == "1" ]]; then
  export HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_POLICY=0
  export HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_CHANGE=0
else
  export HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_POLICY=1
  export HTML_LEARNING_DISABLE_DEFAULT_PASSWORD_CHANGE=1
  export HTML_LEARNING_ALLOW_DEFAULT_PASSWORDS=1
fi
export HACKME_DEV_TRADING_ALLOW_CONSERVATIVE_MARKET_ORDERS=1
export HACKME_DEV_TRADING_ALLOW_UNREADY_MARKETS=1
export HACKME_DEV_TRADING_DISABLE_PRICE_CONFIDENCE_GATES=1
export HACKME_DEV_TRADING_ALLOW_QA_LIVE_PRICE_PROVIDER=1
if [[ -z "${HTML_LEARNING_GIT_REPO_DIR:-}" ]]; then
  if git -C "$DEFAULT_GIT_REPO_DIR" rev-parse HEAD >/dev/null 2>&1; then
    export HTML_LEARNING_GIT_REPO_DIR="$DEFAULT_GIT_REPO_DIR"
  else
    export HTML_LEARNING_GIT_REPO_DIR="$COPY_ROOT"
  fi
fi
export PYTHONPATH="$COPY_ROOT"
export PYTHONPYCACHEPREFIX="$RUNTIME_ROOT/pycache"

cd "$COPY_ROOT"

HACKME_RUNTIME_OUTPUT_CAPTURE=0 "$PYTHON_BIN" - <<'PY'
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import secrets
import server
from services.comfyui.template.seeding import seed_default_comfyui_workflows
from services.server.startup import bootstrap_points_initial_grants_if_due
from services.security.access_controls import (
    generate_internal_test_token,
    hash_internal_test_token,
    maintenance_bypass_expires_at,
)
from services.storage.global_capacity import parse_global_capacity_limit_mb
from services.storage.paths import validate_storage_root
try:
    from services.platform.settings_metadata import setting_detail
except Exception:
    setting_detail = None

server.init_db(
    ensure_secure_audit_columns=server.ensure_secure_audit_columns,
    ensure_user_columns=server.ensure_user_columns,
    ensure_appeal_columns=server.ensure_appeal_columns,
    ensure_session_columns=server.ensure_session_columns,
    ensure_security_support_schema=server.ensure_security_support_schema,
    ensure_points_economy_schema=server.ensure_points_economy_schema,
    ensure_official_chat_room=server.ensure_official_chat_room,
    hash_password=server.hash_password,
)
server.ensure_local_tls_files(server.CERT_FILE, server.KEY_FILE)

feature_keys = [
    key
    for key in server.DEFAULT_SETTINGS
    if key.startswith("feature_")
]
feature_mode = os.environ.get("HACKME_DEV_FEATURE_MODE", "all").strip().lower()
raw_feature_list = os.environ.get("HACKME_DEV_FEATURES", "")
security_enabled = str(os.environ.get("HACKME_DEV_SECURITY_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on", "enabled"}
default_account_passwords = str(os.environ.get("HACKME_DEV_DEFAULT_ACCOUNT_PASSWORDS", "0")).strip().lower() in {"1", "true", "yes", "on", "enabled"}
default_account_must_change = 1 if security_enabled and default_account_passwords else 0


def normalize_feature_key(value):
    value = value.strip()
    if not value:
        return ""
    if not value.startswith("feature_"):
        value = f"feature_{value}"
    return value


def normalize_token_feature_scope(raw_value):
    raw_value = str(raw_value or "").strip()
    if not raw_value or raw_value.lower() in {"all", "*", "unrestricted", "none"}:
        return []
    allowed = []
    unknown = []
    feature_key_set = set(feature_keys)
    for item in raw_value.replace("\n", ",").split(","):
        key = item.strip()
        if not key:
            continue
        if not key.startswith("feature_"):
            key = f"feature_{key}"
        if key not in feature_key_set and not key.endswith("_enabled"):
            maybe_enabled = f"{key}_enabled"
            if maybe_enabled in feature_key_set:
                key = maybe_enabled
        if key not in feature_key_set:
            unknown.append(key)
            continue
        if key not in allowed:
            allowed.append(key)
    if unknown:
        raise SystemExit(f"unknown dev token feature scope: {', '.join(unknown)}")
    return allowed


def feature_label(key):
    if setting_detail is not None:
        try:
            detail = setting_detail(key)
            label = str(detail.get("label") or "").strip()
            if label:
                return label
        except Exception:
            pass
    return key.removeprefix("feature_").removesuffix("_enabled").replace("_", " ")


def dev_token_ttl_minutes():
    try:
        ttl = int(str(os.environ.get("HACKME_DEV_TOKEN_TTL_MINUTES", "1440")).strip())
    except Exception:
        ttl = 1440
    return max(5, min(ttl, 30 * 24 * 60))


def normalize_token_role(raw_value):
    role = str(raw_value or "user").strip() or "user"
    if role not in {"user", "manager", "super_admin"}:
        raise SystemExit(f"invalid generated token account role: {role}")
    return role


selected_features = {
    key
    for key in (normalize_feature_key(item) for item in raw_feature_list.split(","))
    if key
}
if feature_mode == "defaults":
    feature_updates = {}
elif feature_mode in {"custom", "bundles"}:
    feature_updates = {
        key: key in selected_features
        for key in feature_keys
    }
else:
    feature_updates = {
        key: True
        for key in feature_keys
    }
feature_updates["feature_account_security_enabled"] = bool(security_enabled)
relaxed_security_settings = {
    "allow_register": True,
    "audit_chain_enabled": False,
    "audit_chain_reseal_required": False,
    "browser_only_mode_enabled": False,
    "captcha_mode": "none",
    "force_https": False,
    "integrity_guard_enabled": False,
    "integrity_guard_strict_mode": False,
    "ip_blocking_enabled": False,
    "login_violation_enabled": False,
    "max_login_failures": 999999,
    "production_single_account_ip_lock_enabled": False,
    "production_single_ip_account_lock_enabled": False,
    "rate_limit_violation_enabled": False,
    "require_email_verification": False,
    "root_ip_whitelist_enabled": False,
    "server_ssl_enabled": True,
    "session_idle_timeout_minutes": 1440,
    "session_ttl_hours": 168,
}
enabled_security_settings = {
    "allow_register": True,
    "audit_chain_enabled": True,
    "audit_chain_reseal_required": False,
    "browser_only_mode_enabled": False,
    "captcha_mode": "math",
    "force_https": False,
    "integrity_guard_enabled": True,
    "integrity_guard_strict_mode": False,
    "ip_blocking_enabled": True,
    "login_violation_enabled": True,
    "max_login_failures": 8,
    "production_single_account_ip_lock_enabled": False,
    "production_single_ip_account_lock_enabled": False,
    "rate_limit_violation_enabled": True,
    "require_email_verification": False,
    "root_ip_whitelist_enabled": False,
    "server_ssl_enabled": True,
    "session_idle_timeout_minutes": 60,
    "session_ttl_hours": 24,
}
feature_updates.update(enabled_security_settings if security_enabled else relaxed_security_settings)
session_idle_timeout_override = str(os.environ.get("HACKME_DEV_SESSION_IDLE_TIMEOUT_MINUTES", "") or "").strip()
if session_idle_timeout_override:
    feature_updates["session_idle_timeout_minutes"] = int(session_idle_timeout_override)
feature_updates.update({
    "server_timezone": os.environ.get("HACKME_DEV_SERVER_TIMEZONE") or os.environ.get("TZ") or "Asia/Taipei",
    # Dev default: keep ComfyUI in remote mode unless a local override is explicit.
    "comfyui_connection_mode": os.environ.get("HACKME_DEV_COMFYUI_CONNECTION_MODE") or "remote",
    "comfyui_remote_api_url": os.environ.get("HACKME_DEV_COMFYUI_REMOTE_API_URL") or os.environ.get("COMFYUI_API_URL") or "http://192.168.18.18:8188",
    "comfyui_base_dir": os.environ.get("HACKME_DEV_COMFYUI_BASE_DIR") or "",
    "comfyui_local_start_script": os.environ.get("HACKME_DEV_COMFYUI_LOCAL_START_SCRIPT") or "",
})
cloud_drive_setting_updates = {}
cloud_drive_storage_root = str(os.environ.get("HACKME_DEV_CLOUD_DRIVE_STORAGE_ROOT", "") or "").strip()
if cloud_drive_storage_root:
    cloud_drive_setting_updates["cloud_drive_storage_root"] = str(
        validate_storage_root(cloud_drive_storage_root, base_dir=server.BASE_DIR, create=True)
    )
cloud_drive_capacity_limit = str(os.environ.get("HACKME_DEV_CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB", "") or "").strip()
if cloud_drive_capacity_limit:
    cloud_drive_setting_updates["cloud_drive_global_capacity_limit_mb"] = parse_global_capacity_limit_mb(
        cloud_drive_capacity_limit
    )
feature_updates.update(cloud_drive_setting_updates)
server.save_settings(feature_updates)


def parse_extra_accounts(raw_value):
    accounts = []
    for spec in (raw_value or "").split(","):
        spec = spec.strip()
        if not spec:
            continue
        parts = spec.split(":", 2)
        if len(parts) < 2 or not parts[0].strip() or not parts[1]:
            raise SystemExit(f"invalid extra account spec: {spec!r}; expected username:password[:role]")
        role = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else "user"
        if role not in {"user", "manager", "super_admin"}:
            raise SystemExit(f"invalid role for extra account {parts[0]!r}: {role}")
        accounts.append((parts[0].strip(), parts[1], role))
    return accounts


def ensure_extra_account(conn, username, password, role, now):
    member_level = "trusted" if role == "user" else "normal"
    row = conn.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if row:
        user_id = row["id"]
        conn.execute(
            """
            UPDATE users
            SET status='active',
                role=?,
                must_change_password=0,
                is_default_password=0,
                failed_login_count=0,
                locked_until=NULL,
                blocked_until=NULL,
                member_level=?,
                base_level=?,
                effective_level=?,
                updated_at=?
            WHERE id=?
            """,
            (role, member_level, member_level, member_level, now, user_id),
        )
    else:
        cur = conn.execute(
            """
            INSERT INTO users
                (username, status, role, member_level, base_level, effective_level, created_at, updated_at)
            VALUES
                (?, 'active', ?, ?, ?, ?, ?, ?)
            """,
            (username, role, member_level, member_level, member_level, now, now),
        )
        user_id = cur.lastrowid
    conn.execute(
        "INSERT INTO user_passwords (user_id, password_hash, created_at) VALUES (?, ?, ?)",
        (user_id, server.hash_password(password), now),
    )


def ensure_dev_token_account(conn, now):
    username = str(os.environ.get("HACKME_DEV_TOKEN_USER", "test") or "test").strip()
    if not username:
        raise SystemExit("generated token account username cannot be blank")
    role = normalize_token_role(os.environ.get("HACKME_DEV_TOKEN_ROLE", "user"))
    configured_password = str(os.environ.get("HACKME_DEV_TOKEN_PASSWORD", "") or "")
    password_to_report = ""
    created = False
    updated_password = False

    row = conn.execute("SELECT id, username, role FROM users WHERE username=? LIMIT 1", (username,)).fetchone()
    if row:
        if configured_password:
            ensure_extra_account(conn, username, configured_password, role, now)
            updated_password = True
            password_to_report = configured_password
    else:
        password = configured_password or secrets.token_urlsafe(12)
        ensure_extra_account(conn, username, password, role, now)
        created = True
        password_to_report = password

    row = conn.execute("SELECT id, username, role FROM users WHERE username=? LIMIT 1", (username,)).fetchone()
    if not row:
        raise SystemExit(f"generated token account could not be created: {username}")
    return row, {
        "username": str(row["username"] or username),
        "role": str(row["role"] or role),
        "created": created,
        "password_updated": updated_password,
        "password": password_to_report,
    }


conn = server.get_db()
try:
    server.ensure_trading_schema(conn)
    now = datetime.now().isoformat()
    selected_server_mode = os.environ.get("HACKME_DEV_SERVER_MODE", "dev_ready").strip() or "dev_ready"
    def apply_selected_server_mode(mode_conn):
        changed = mode_conn.execute(
            """
            UPDATE server_modes
            SET previous_mode=CASE WHEN current_mode<>? THEN current_mode ELSE previous_mode END,
                current_mode=?,
                mode_changed_at=?,
                notes=?,
                reason=?,
                config_json=?
            WHERE id=1
            """,
            (
                selected_server_mode,
                selected_server_mode,
                now,
                "test_for_develop.sh",
                "dev runtime bootstrap",
                json.dumps(
                    {
                        "source": "test_for_develop.sh",
                        "security_enabled": security_enabled,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            ),
        ).rowcount
        if not changed:
            mode_conn.execute(
                """
                INSERT INTO server_modes
                    (id, current_mode, previous_mode, active_snapshot_id, checkpoint_id,
                     mode_changed_by, mode_changed_at, notes, reason, config_json)
                VALUES
                    (1, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    selected_server_mode,
                    now,
                    "test_for_develop.sh",
                    "dev runtime bootstrap",
                    json.dumps({"source": "test_for_develop.sh", "security_enabled": security_enabled}, ensure_ascii=True, sort_keys=True),
                ),
            )
    try:
        apply_selected_server_mode(conn)
        control_conn = server.get_control_db()
        try:
            apply_selected_server_mode(control_conn)
            control_conn.commit()
        finally:
            control_conn.close()
    except Exception:
        pass
    conn.execute("DELETE FROM ip_blocks")
    conn.execute("DELETE FROM security_events")
    conn.execute("DELETE FROM notifications WHERE type='root_security_alert'")
    conn.execute("UPDATE sessions SET is_revoked=1, revoked_at=?", (now,))
    conn.execute(
        """
        UPDATE users
        SET must_change_password=?,
            is_default_password=?,
            failed_login_count=0,
            locked_until=NULL,
            blocked_until=NULL,
            updated_at=?
        WHERE username IN ('root', 'admin', 'test')
        """,
        (default_account_must_change, default_account_must_change, now),
    )
    for username, password, role in parse_extra_accounts(os.environ.get("HACKME_DEV_EXTRA_ACCOUNTS", "")):
        ensure_extra_account(conn, username, password, role, now)
    conn.execute(
        """
        UPDATE trading_markets_registry
        SET enabled=1,
            allow_spot=1,
            allow_margin=1,
            allow_bots=1,
            allow_risk_grade_usage=1,
            live_price_enabled=1,
            reference_price_enabled=1,
            updated_at=?
        """,
        (now,),
    )
    conn.execute(
        """
        UPDATE trading_markets
        SET enabled=1,
            allow_margin=1,
            allow_bots=1,
            allow_risk_grade_usage=1,
            live_price_enabled=1,
            reference_price_enabled=1,
            updated_at=?
        """,
        (now,),
    )
    for key, value in (
        ("trading.enabled", "true"),
        ("trading.borrowing_enabled", "true"),
        ("trading.margin_liquidation_enabled", "true"),
        ("trading.bot_auto_scan_enabled", "true"),
        ("trading.bot_audit_enabled", "true"),
        ("trading.background_worker_dev_ready_enabled", "true" if os.environ.get("HACKME_DEV_TRADING_BACKGROUND_DEV_READY", "0").strip().lower() in {"1", "true", "yes", "on", "y"} else "false"),
        ("background_worker_dev_ready_enabled", "true" if os.environ.get("HACKME_DEV_TRADING_BACKGROUND_DEV_READY", "0").strip().lower() in {"1", "true", "yes", "on", "y"} else "false"),
        ("trading.price_degrade_pause_market_orders", "false"),
        ("trading.price_degrade_pause_bots", "false"),
        ("trading.price_degrade_pause_borrowing", "false"),
        ("trading.allow_unready_markets", "true"),
        ("trading.disable_price_confidence_gates", "true"),
        ("trading.dev_allow_conservative_market_orders", "true"),
        ("trading.dev_allow_unready_markets", "true"),
        ("trading.dev_disable_price_confidence_gates", "true"),
        ("trading.warning_language", "zh-TW"),
        ("trading.simulated_slippage_enabled", "false"),
        ("trading.simulated_slippage_base_basis_points", "0"),
        ("trading.simulated_slippage_size_basis_points_per_10k_notional", "0"),
        ("trading.simulated_slippage_max_basis_points", "0"),
        # Dev default: pin price fusion to Binance public API only so the
        # /tmp dev runtime does not require OKX/Coinbase/Kraken/Gemini/Bitstamp
        # reachability for spot trading, live-price, or risk-grade gating.
        ("trading.price_fusion_mode", "manual_weights"),
        (
            "trading.price_fusion_manual_weights_json",
            '{"binance_public_api": 100.0, "okx_public_api": 0.0, '
            '"coinbase_exchange": 0.0, "kraken_public_api": 0.0, '
            '"gemini_public_api": 0.0, "bitstamp_public_api": 0.0}',
        ),
        ("trading.price_fusion_min_provider_count", "1"),
        ("trading.price_fusion_trade_min_provider_count", "1"),
        # Lift the single-provider cap to 100% so dev runtime can run on
        # Binance alone without the provider_weight_cap_unenforceable warning.
        ("trading.price_fusion_max_single_provider_weight_percent", "100"),
        # Dev default: pin BTC_trade prediction engine on, parked at the
        # shared /tmp/BTC_trade workspace so multiple dev runs reuse the same
        # cloned repo, downloaded data and trained models. The signal pipeline
        # itself (clone → install → predict → retrain) is kicked off in the
        # background after the server URL becomes available; see the autostart
        # block further down.
        ("trading.btc_trade_enabled", "true"),
        ("trading.btc_trade_project_dir", "/tmp/BTC_trade"),
    ):
        conn.execute(
            "INSERT OR REPLACE INTO trading_settings (key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            (key, value, now, "test_for_develop"),
        )
    conn.commit()
finally:
    conn.close()

try:
    comfyui_seed = seed_default_comfyui_workflows(runtime_root=Path(os.environ["HACKME_RUNTIME_DIR"]))
    print(
        "[dev-tmp] workflows seeded: "
        f"source={comfyui_seed.get('source_count', 0)} "
        f"runtime={comfyui_seed.get('runtime_count', 0)} "
        f"copied={len(comfyui_seed.get('copied') or [])} "
        f"destination={comfyui_seed.get('destination')}"
    )
except Exception as exc:
    print(f"[dev-tmp] warning: official ComfyUI workflow seed failed: {exc}")

points_bootstrap = bootstrap_points_initial_grants_if_due(
    points_service=server.points_service,
    get_system_settings=server.get_system_settings,
    get_runtime_server_mode=server.get_runtime_server_mode,
    audit=server.audit,
    env_value="1" if selected_server_mode in {"production", "dev_ready", "test"} else "",
)
if not points_bootstrap.get("ok"):
    print(f"[dev-tmp] warning: default account point grants failed: {points_bootstrap.get('error')}")
elif not points_bootstrap.get("skipped"):
    genesis = points_bootstrap.get("genesis") or {}
    if genesis.get("created_count"):
        print(f"[dev-tmp] default account point grants created: {genesis.get('created_count')}")

dev_tokens_path = os.environ.get("HACKME_DEV_TOKENS_FILE", "").strip()
dev_tokens_payload = {
    "ok": True,
    "server_mode": selected_server_mode,
    "tokens": {},
    "warnings": [],
}
if selected_server_mode in {"test", "internal_test"} and dev_tokens_path:
    ttl_minutes = dev_token_ttl_minutes()
    token_features = normalize_token_feature_scope(os.environ.get("HACKME_DEV_TOKEN_FEATURES", ""))
    effective_feature_values = dict(server.DEFAULT_SETTINGS)
    effective_feature_values.update(feature_updates)
    dev_tokens_payload["token_feature_scope"] = "unrestricted" if not token_features else "restricted"
    dev_tokens_payload["allowed_feature_keys"] = token_features
    dev_tokens_payload["available_feature_keys"] = [
        {
            "key": key,
            "label": feature_label(key),
            "enabled": bool(effective_feature_values.get(key, False)),
            "allowed_by_token": (not token_features or key in token_features),
        }
        for key in feature_keys
    ]
    token_user = None
    token_user_info = {}
    conn = server.get_db()
    try:
        token_user, token_user_info = ensure_dev_token_account(conn, datetime.now().isoformat())
        conn.commit()
    finally:
        conn.close()
    dev_tokens_payload["token_user"] = token_user_info
    if token_user:
        user_id = int(token_user["id"])
        username = str(token_user["username"] or token_user_info.get("username") or "test")
        if selected_server_mode == "internal_test":
            login_token = generate_internal_test_token()
            login_expires_at = maintenance_bypass_expires_at(ttl_minutes)
            server.save_settings({
                "internal_test_login_token_hash": hash_internal_test_token(login_token),
                "internal_test_login_token_expires_at": login_expires_at,
                "internal_test_login_token_user_id": user_id,
                "internal_test_login_token_username": username,
                "internal_test_login_token_allowed_features_json": json.dumps(token_features, ensure_ascii=True, sort_keys=True),
            })
            dev_tokens_payload["tokens"]["internal_test_login_token"] = {
                "token": login_token,
                "target_user_id": user_id,
                "target_username": username,
                "expires_at": login_expires_at,
                "ttl_minutes": ttl_minutes,
                "allowed_features": token_features,
                "usage": "login as the bound user in internal_test mode with username + internal_test_token/login_token/X-Internal-Test-Token; password may be blank",
            }
        if hasattr(server, "server_mode_service"):
            tester_expires_at = (datetime.now() + timedelta(minutes=ttl_minutes)).replace(microsecond=0).isoformat()
            tester_result = server.server_mode_service.create_tester_token(
                actor={"id": 1, "username": "root", "role": "super_admin"},
                tester_user_id=user_id,
                allowed_features=token_features,
                allowed_routes=[],
                expires_at=tester_expires_at,
                max_requests_per_minute=120,
                can_modify_own_role=False,
                can_modify_own_points=False,
                can_run_security_tests=False,
            )
            if tester_result.get("ok"):
                dev_tokens_payload["tokens"]["tester_token"] = {
                    "token": tester_result.get("token"),
                    "token_id": tester_result.get("token_id"),
                    "user_id": user_id,
                    "username": username,
                    "expires_at": tester_result.get("expires_at") or tester_expires_at,
                    "ttl_minutes": ttl_minutes,
                    "allowed_features": token_features,
                    "usage": "login as the bound user in test/internal_test mode with username + tester_token/login_token/X-Tester-Token; password may be blank",
                }
            else:
                dev_tokens_payload["warnings"].append(f"tester token generation failed: {tester_result.get('msg') or tester_result}")
    else:
        dev_tokens_payload["warnings"].append("generated token account was not found; no dev token generated")
    os.makedirs(os.path.dirname(dev_tokens_path) or ".", exist_ok=True)
    tmp_path = f"{dev_tokens_path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(dev_tokens_payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    try:
        os.chmod(tmp_path, 0o600)
    except Exception:
        pass
    os.replace(tmp_path, dev_tokens_path)
PY

if [[ "$FOREGROUND" == "1" ]]; then
  if [[ "$RUNTIME_IN_SOURCE" == "1" ]]; then
    say "[dev-tmp] source:    $COPY_ROOT (source runtime deployment)"
  elif [[ "$IN_PLACE" == "1" ]]; then
    say "[dev-tmp] source:    $COPY_ROOT (in-place, no copy; tmp runtime)"
  else
    say "[dev-tmp] repo copy: $COPY_ROOT"
  fi
  say "[dev-tmp] runtime:   $RUNTIME_ROOT"
  say "[dev-tmp] storage:   $EFFECTIVE_STORAGE_ROOT"
  if [[ -n "$CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB" ]]; then
    say "[dev-tmp] storage cap: ${CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB} MB"
  fi
  if [[ -n "$MAX_CONTENT_MB" ]]; then
    say "[dev-tmp] max content: ${MAX_CONTENT_MB} MB"
  fi
  say "[dev-tmp] mode:      foreground $SERVER_RUNNER"
  if [[ "$SERVER_RUNNER" == "flask" ]]; then
    say "[dev-tmp] warning:   Flask/Werkzeug direct server is debug-only; use gunicorn for uploads/HLS/load."
  fi
  say "[dev-tmp] accounts:   root/${ROOT_PASSWORD} admin/${MANAGER_PASSWORD} test/${TEST_PASSWORD}"
  print_transmission_access_summary
  print_generated_dev_tokens
  write_restart_shortcut_script
  if [[ "$SERVER_RUNNER" == "gunicorn" ]]; then
    exec "$PYTHON_BIN" -m gunicorn "server:app" \
      --bind "${HOST}:${PORT}" \
      --worker-class gthread \
      --workers "$GUNICORN_WORKERS" \
      --threads "$GUNICORN_THREADS" \
      --timeout "$GUNICORN_TIMEOUT" \
      --graceful-timeout "$GUNICORN_GRACEFUL_TIMEOUT" \
      --keep-alive "$GUNICORN_KEEP_ALIVE" \
      --backlog "$GUNICORN_BACKLOG" \
      --max-requests "$GUNICORN_MAX_REQUESTS" \
      --max-requests-jitter "$GUNICORN_MAX_REQUESTS_JITTER" \
      --certfile "$RUNTIME_ROOT/cert.pem" \
      --keyfile "$RUNTIME_ROOT/key.pem" \
      --access-logfile - \
      --error-logfile -
  fi
  exec "$PYTHON_BIN" server.py
fi

if [[ "$SERVER_RUNNER" == "gunicorn" ]]; then
  setsid "$PYTHON_BIN" -m gunicorn "server:app" \
    --bind "${HOST}:${PORT}" \
    --worker-class gthread \
    --workers "$GUNICORN_WORKERS" \
    --threads "$GUNICORN_THREADS" \
    --timeout "$GUNICORN_TIMEOUT" \
    --graceful-timeout "$GUNICORN_GRACEFUL_TIMEOUT" \
    --keep-alive "$GUNICORN_KEEP_ALIVE" \
    --backlog "$GUNICORN_BACKLOG" \
    --max-requests "$GUNICORN_MAX_REQUESTS" \
    --max-requests-jitter "$GUNICORN_MAX_REQUESTS_JITTER" \
    --certfile "$RUNTIME_ROOT/cert.pem" \
    --keyfile "$RUNTIME_ROOT/key.pem" \
    --access-logfile "$GUNICORN_ACCESS_LOG" \
    --error-logfile "$GUNICORN_ERROR_LOG" >"$LOG_CAPTURE" 2>&1 < /dev/null &
else
  say "[dev-tmp] warning:   Flask/Werkzeug direct server is debug-only; use gunicorn for uploads/HLS/load."
  setsid "$PYTHON_BIN" server.py >"$LOG_CAPTURE" 2>&1 < /dev/null &
fi
SERVER_PID="$!"
printf '%s\n' "$SERVER_PID" > "$PID_FILE"

SERVER_URL="$(wait_for_server_url || true)"

if [[ "$RUNTIME_IN_SOURCE" == "1" ]]; then
  say "[dev-tmp] source:    $COPY_ROOT (source runtime deployment)"
elif [[ "$IN_PLACE" == "1" ]]; then
  say "[dev-tmp] source:    $COPY_ROOT (in-place, no copy; tmp runtime)"
else
  say "[dev-tmp] repo copy: $COPY_ROOT"
fi
say "[dev-tmp] runtime:   $RUNTIME_ROOT"
say "[dev-tmp] storage:   $EFFECTIVE_STORAGE_ROOT"
if [[ -n "$CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB" ]]; then
  say "[dev-tmp] storage cap: ${CLOUD_DRIVE_GLOBAL_CAPACITY_LIMIT_MB} MB"
fi
if [[ -n "$MAX_CONTENT_MB" ]]; then
  say "[dev-tmp] max content: ${MAX_CONTENT_MB} MB"
fi
say "[dev-tmp] pid:       $SERVER_PID"
say "[dev-tmp] runner:    $SERVER_RUNNER"
if [[ "$DISABLE_TRUSTED_HOSTS" == "1" ]]; then
  say "[dev-tmp] trusted:   disabled by --allow-any-host (dev only)"
elif [[ -n "$TRUSTED_HOSTS" ]]; then
  say "[dev-tmp] trusted:   $TRUSTED_HOSTS"
fi
if [[ -n "$SERVER_URL" ]]; then
  say "[dev-tmp] url:       $SERVER_URL"
  if [[ -n "$PUBLIC_HOST" ]]; then
    case "$PUBLIC_HOST" in
      \[*\]|*:*:*)
        say "[dev-tmp] public:    https://$PUBLIC_HOST"
        ;;
      *:*)
        say "[dev-tmp] public:    https://$PUBLIC_HOST"
        ;;
      *)
        say "[dev-tmp] public:    https://$PUBLIC_HOST:$PORT"
        ;;
    esac
  fi
else
  say "[dev-tmp] url:       startup pending; inspect logs"
fi
say "[dev-tmp] accounts:   root/${ROOT_PASSWORD} admin/${MANAGER_PASSWORD} test/${TEST_PASSWORD}"
print_transmission_access_summary
if [[ "$FOREGROUND" == "1" ]]; then
  say "[dev-tmp] log:       foreground mode uses stdout/stderr"
elif [[ "$SERVER_RUNNER" == "gunicorn" ]]; then
  say "[dev-tmp] log:       $LOG_CAPTURE"
  say "[dev-tmp] access:    $GUNICORN_ACCESS_LOG"
  say "[dev-tmp] error:     $GUNICORN_ERROR_LOG"
else
  say "[dev-tmp] log:       $LOG_CAPTURE"
fi
print_generated_dev_tokens
if [[ -n "$SERVER_URL" ]]; then
  write_restart_shortcut_script
else
  say "[dev-tmp] shortcut: skipped until server startup succeeds"
fi

# BTC_trade autostart: kick the prediction pipeline off in the background
# so the trading dashboard already has live BTC_trade signal data on first
# page load. The server-side job uses setup_if_needed=True, so:
#   - first run: clones BTC_trade into /tmp/BTC_trade, installs deps,
#     trains the model, then predicts.
#   - re-run when /tmp/BTC_trade already has the required scripts: skips
#     clone/install and goes straight to update_data → retrain → predict.
# This is intentionally fire-and-forget so test_for_develop.sh exits fast.
if [[ -n "$SERVER_URL" && "$BTC_TRADE_AUTOSTART" == "1" ]]; then
  BTC_LOG="$RUNTIME_ROOT/logs/btc_trade_autostart.log"
  (
    set +e
    sleep 2
    JAR="$(mktemp)"
    LOGIN_BODY="$("$PYTHON_BIN" -c 'import json,os; print(json.dumps({"username":"root","password":os.environ["HTML_LEARNING_ROOT_PASSWORD"]}))')"
    csrf="$(curl -ksS -c "$JAR" "$SERVER_URL/api/csrf-token" | "$PYTHON_BIN" -c 'import json,sys;print(json.load(sys.stdin).get("csrf_token",""))')"
    curl -ksS -c "$JAR" -b "$JAR" \
      -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
      -X POST "$SERVER_URL/api/login" -d "$LOGIN_BODY" >/dev/null
    csrf="$(curl -ksS -c "$JAR" -b "$JAR" "$SERVER_URL/api/csrf-token" | "$PYTHON_BIN" -c 'import json,sys;print(json.load(sys.stdin).get("csrf_token",""))')"
    echo "[btc_trade_autostart] POST /api/root/trading/btc-trade/start"
    curl -ksS -b "$JAR" \
      -H "Content-Type: application/json" -H "X-CSRF-Token: $csrf" \
      -X POST "$SERVER_URL/api/root/trading/btc-trade/start" \
      -d '{"timeframe":"4h"}'
    echo
    rm -f "$JAR"
  ) > "$BTC_LOG" 2>&1 &
  say "[dev-tmp] btc_trade: autostart kicked off in background (log: $BTC_LOG)"
elif [[ -n "$SERVER_URL" ]]; then
  say "[dev-tmp] btc_trade: autostart disabled"
fi
