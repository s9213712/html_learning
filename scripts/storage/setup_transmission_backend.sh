#!/usr/bin/env bash
set -euo pipefail

show_usage() {
  cat <<'USAGE'
Usage:
  sudo bash scripts/storage/setup_transmission_backend.sh --storage-root /path/to/runtime/storage [options]

Options:
  --storage-root PATH       Cloud Drive storage root. Required unless HTML_LEARNING_STORAGE_DIR is set.
  --settings-file PATH      Transmission settings.json path. Default: /etc/transmission-daemon/settings.json
  --service NAME            systemd service name. Default: transmission-daemon
  --transmission-user USER  Transmission daemon user. Default: debian-transmission if present, else transmission
  --app-user USER           hackme_web app user. Default: SUDO_USER or current user
  --rpc-username USER       RPC username. Default: hackme_web
  --rpc-password PASS       RPC password. Default: generated random password
  --rpc-port PORT           RPC port. Default: 9091
  --rpc-bind-address ADDR   RPC bind address. Default: 127.0.0.1
  --rpc-whitelist LIST      RPC IP whitelist. Default: 127.0.0.1,::1
  --rpc-whitelist-enabled VALUE
                           Enable RPC IP whitelist: true/false. Default: true
  --allow-any-rpc-ip        Listen on 0.0.0.0 and allow RPC login from any IP.
                           Authentication remains required.
  --no-restart              Do not restart transmission-daemon after writing settings.
  --no-systemd-override     Do not install the Type=simple systemd timeout workaround.
  -h, --help                Show this help.

The script backs up settings.json before writing. It prints the RPC values that
must be copied into hackme_web root system settings after the daemon is ready.
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "missing command: $1" >&2; exit 1; }
}

as_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Run with sudo/root because Transmission settings and service files need root writes." >&2
    exit 1
  fi
}

bool_is_false() {
  case "${1,,}" in
    0|false|no|n|off|disable|disabled) return 0 ;;
    *) return 1 ;;
  esac
}

storage_root="${HTML_LEARNING_STORAGE_DIR:-}"
settings_file="/etc/transmission-daemon/settings.json"
service_name="transmission-daemon"
transmission_user=""
app_user="${SUDO_USER:-$(id -un)}"
rpc_username="hackme_web"
rpc_password=""
rpc_port="9091"
rpc_bind_address="127.0.0.1"
rpc_whitelist="127.0.0.1,::1"
rpc_whitelist_enabled="true"
allow_any_rpc_ip="0"
restart_service="1"
install_systemd_override="1"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --storage-root) storage_root="${2:?missing --storage-root value}"; shift 2 ;;
    --settings-file) settings_file="${2:?missing --settings-file value}"; shift 2 ;;
    --service) service_name="${2:?missing --service value}"; shift 2 ;;
    --transmission-user) transmission_user="${2:?missing --transmission-user value}"; shift 2 ;;
    --app-user) app_user="${2:?missing --app-user value}"; shift 2 ;;
    --rpc-username) rpc_username="${2:?missing --rpc-username value}"; shift 2 ;;
    --rpc-password) rpc_password="${2:?missing --rpc-password value}"; shift 2 ;;
    --rpc-port) rpc_port="${2:?missing --rpc-port value}"; shift 2 ;;
    --rpc-bind-address) rpc_bind_address="${2:?missing --rpc-bind-address value}"; shift 2 ;;
    --rpc-whitelist) rpc_whitelist="${2:?missing --rpc-whitelist value}"; shift 2 ;;
    --rpc-whitelist-enabled) rpc_whitelist_enabled="${2:?missing --rpc-whitelist-enabled value}"; shift 2 ;;
    --allow-any-rpc-ip|--rpc-allow-any-ip)
      allow_any_rpc_ip="1"
      rpc_bind_address="0.0.0.0"
      rpc_whitelist_enabled="false"
      rpc_whitelist="*.*.*.*"
      shift
      ;;
    --no-restart) restart_service="0"; shift ;;
    --no-systemd-override) install_systemd_override="0"; shift ;;
    -h|--help) show_usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; show_usage >&2; exit 1 ;;
  esac
done

as_root
need_cmd python3
need_cmd systemctl
need_cmd getent
need_cmd usermod
need_cmd chgrp
need_cmd chmod

if [[ -z "$storage_root" ]]; then
  echo "--storage-root is required unless HTML_LEARNING_STORAGE_DIR is set." >&2
  exit 1
fi
if [[ ! -f "$settings_file" ]]; then
  echo "Transmission settings file not found: $settings_file" >&2
  echo "Install/start transmission-daemon once first, or pass --settings-file." >&2
  exit 1
fi
if [[ -z "$transmission_user" ]]; then
  if id debian-transmission >/dev/null 2>&1; then
    transmission_user="debian-transmission"
  elif id transmission >/dev/null 2>&1; then
    transmission_user="transmission"
  else
    echo "Cannot detect Transmission daemon user. Pass --transmission-user." >&2
    exit 1
  fi
fi
if ! id "$app_user" >/dev/null 2>&1; then
  echo "App user not found: $app_user. Pass --app-user." >&2
  exit 1
fi
if ! id "$transmission_user" >/dev/null 2>&1; then
  echo "Transmission user not found: $transmission_user" >&2
  exit 1
fi
if [[ -z "$rpc_password" ]]; then
  if command -v openssl >/dev/null 2>&1; then
    rpc_password="$(openssl rand -base64 24 | tr -d '\n' | tr '/+' '_-' | cut -c1-24)"
  else
    rpc_password="$(python3 - <<'PYGEN'
import secrets
print(secrets.token_urlsafe(18))
PYGEN
)"
  fi
fi

storage_root="$(python3 - <<'PYPATH' "$storage_root"
import os, sys
print(os.path.abspath(os.path.expanduser(sys.argv[1])))
PYPATH
)"
download_dir="$storage_root/_runtime/remote-downloads/transmission-daemon-downloads"
staging_dir="$storage_root/_runtime/remote-downloads/transmission-staging"
incomplete_dir="$storage_root/_runtime/remote-downloads/transmission-incomplete"
shared_group="hackme-bt"
timestamp="$(date +%Y%m%d-%H%M%S)"
backup_file="${settings_file}.bak-${timestamp}"

cat <<INFO
[transmission-setup] storage_root:      $storage_root
[transmission-setup] settings_file:     $settings_file
[transmission-setup] backup_file:       $backup_file
[transmission-setup] app_user:          $app_user
[transmission-setup] transmission_user: $transmission_user
[transmission-setup] shared_group:      $shared_group
[transmission-setup] rpc_bind_address:  $rpc_bind_address
[transmission-setup] rpc_whitelist:     $(bool_is_false "$rpc_whitelist_enabled" && printf 'disabled (any IP)' || printf '%s' "$rpc_whitelist")
[transmission-setup] download_dir:      $download_dir
[transmission-setup] staging_dir:       $staging_dir
INFO

if systemctl list-unit-files "$service_name.service" >/dev/null 2>&1; then
  echo "[transmission-setup] stopping $service_name before writing settings"
  systemctl stop "$service_name" || true
fi

cp -a "$settings_file" "$backup_file"
echo "[transmission-setup] backed up settings before write: $backup_file"

if ! getent group "$shared_group" >/dev/null 2>&1; then
  groupadd --system "$shared_group"
fi
usermod -aG "$shared_group" "$app_user"
usermod -aG "$shared_group" "$transmission_user"

install -d -m 2770 -o "$app_user" -g "$shared_group" "$storage_root/_runtime" "$storage_root/_runtime/remote-downloads"
install -d -m 2770 -o "$transmission_user" -g "$shared_group" "$download_dir" "$incomplete_dir"
install -d -m 2770 -o "$app_user" -g "$shared_group" "$staging_dir"
chgrp -R "$shared_group" "$storage_root/_runtime/remote-downloads"
chmod -R g+rwX "$storage_root/_runtime/remote-downloads"
find "$storage_root/_runtime/remote-downloads" -type d -exec chmod 2770 {} +

if command -v setfacl >/dev/null 2>&1; then
  setfacl -R -m "u:${app_user}:rwx,u:${transmission_user}:rwx,g:${shared_group}:rwx" "$storage_root/_runtime/remote-downloads" || true
  setfacl -R -d -m "u:${app_user}:rwx,u:${transmission_user}:rwx,g:${shared_group}:rwx" "$storage_root/_runtime/remote-downloads" || true
  python3 - <<'PYACL' "$storage_root" "$transmission_user"
import os
import subprocess
import sys
root = os.path.abspath(sys.argv[1])
user = sys.argv[2]
current = root
seen = []
while True:
    seen.append(current)
    parent = os.path.dirname(current)
    if parent == current:
        break
    current = parent
for path in reversed(seen):
    if path == "/":
        continue
    subprocess.run(["setfacl", "-m", f"u:{user}:--x", path], check=False)
PYACL
  setfacl -m "u:${transmission_user}:rwx,u:${app_user}:rwx,g:${shared_group}:rwx" "$storage_root" || true
  echo "[transmission-setup] applied parent-directory traverse ACL for ${transmission_user}"
else
  echo "[transmission-setup] setfacl not found; group+setgid permissions were applied, but parent directories may still block ${transmission_user}. Install package 'acl' or grant execute ACL manually."
fi

if [[ "$install_systemd_override" == "1" ]]; then
  override_dir="/etc/systemd/system/${service_name}.service.d"
  install -d -m 0755 "$override_dir"
  cat >"${override_dir}/hackme-web.conf" <<'OVERRIDE'
[Service]
Type=simple
OVERRIDE
  echo "[transmission-setup] installed systemd Type=simple override: ${override_dir}/hackme-web.conf"
fi

python3 - <<'PYSET' "$settings_file" "$download_dir" "$incomplete_dir" "$rpc_username" "$rpc_password" "$rpc_port" "$rpc_bind_address" "$rpc_whitelist_enabled" "$rpc_whitelist" "$allow_any_rpc_ip"
import json
import sys
from pathlib import Path

def parse_bool(raw, *, name):
    value = str(raw or "").strip().lower()
    if value in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
        return True
    if value in {"0", "false", "no", "n", "off", "disable", "disabled"}:
        return False
    print(f"{name} must be true/false or 1/0: {raw}", file=sys.stderr)
    raise SystemExit(2)

settings_path = Path(sys.argv[1])
(
    download_dir,
    incomplete_dir,
    rpc_username,
    rpc_password,
    rpc_port,
    rpc_bind_address,
    rpc_whitelist_enabled,
    rpc_whitelist,
    allow_any_rpc_ip,
) = sys.argv[2:11]
rpc_whitelist_enabled_bool = parse_bool(rpc_whitelist_enabled, name="rpc-whitelist-enabled")
data = json.loads(settings_path.read_text(encoding='utf-8'))
data.update({
    'download-dir': download_dir,
    'incomplete-dir': incomplete_dir,
    'incomplete-dir-enabled': True,
    'rpc-enabled': True,
    'rpc-bind-address': rpc_bind_address,
    'rpc-port': int(rpc_port),
    'rpc-authentication-required': True,
    'rpc-username': rpc_username,
    'rpc-password': rpc_password,
    'rpc-whitelist-enabled': rpc_whitelist_enabled_bool,
    'rpc-whitelist': rpc_whitelist,
    'umask': 2,
})
if allow_any_rpc_ip == "1":
    data['rpc-host-whitelist-enabled'] = False
settings_path.write_text(json.dumps(data, indent=4, sort_keys=True) + '\n', encoding='utf-8')
PYSET
chown "$transmission_user":"$shared_group" "$settings_file"
chmod 640 "$settings_file"

echo "[transmission-setup] wrote Transmission settings"
if [[ "$restart_service" == "1" ]]; then
  systemctl daemon-reload
  systemctl enable --now "$service_name"
  systemctl restart "$service_name"
  echo "[transmission-setup] restarted $service_name"
fi

cat <<RESULT

Copy these values into hackme_web root system settings:
  BT/magnet backend:           transmission or auto
  Transmission RPC URL:        http://127.0.0.1:${rpc_port}/transmission/rpc
  Transmission RPC username:   ${rpc_username}
  Transmission RPC password:   ${rpc_password}
  Transmission RPC bind:       ${rpc_bind_address}
  Transmission RPC IP access:  $(bool_is_false "$rpc_whitelist_enabled" && printf 'any IP (whitelist disabled)' || printf 'whitelist %s' "$rpc_whitelist")

Also set this env for app-side per-user/per-task staging:
  HACKME_BT_DOWNLOAD_STAGING_DIR=${staging_dir}

Important:
  - The password above is an initial generated value. Change it yourself in
    ${settings_file}, restart ${service_name}, then update the same value in
    the hackme_web root frontend.
  - Existing login sessions for ${app_user} or ${transmission_user} may need
    re-login/restart to see the new ${shared_group} membership.
  - A systemd Type=simple drop-in may have been installed because some Ubuntu
    Transmission builds start correctly but never send READY=1 to Type=notify.
  - If RPC bind is 0.0.0.0, hackme_web can still use the local RPC URL above;
    remote clients should use http://<server-ip>:${rpc_port}/transmission/rpc.
    Keep RPC behind LAN/VPN/firewall and keep authentication enabled.
  - Backup created before writing: ${backup_file}

Quick RPC check:
  transmission-remote 127.0.0.1:${rpc_port} --auth '${rpc_username}:<password>' --session-info
RESULT
