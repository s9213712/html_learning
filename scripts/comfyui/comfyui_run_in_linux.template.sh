#!/usr/bin/env bash
set -euo pipefail

# Copy this file into a ComfyUI portable install and edit the values to fit
# the local environment. It is a template, not a repo-side wrapper.

COMFYUI_ROOT="${COMFYUI_ROOT:-$HOME/ComfyUI}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
LISTEN_HOST="${LISTEN_HOST:-0.0.0.0}"
LISTEN_PORT="${LISTEN_PORT:-8192}"

EXTRA_ARGS=("$@")
if [[ ${#EXTRA_ARGS[@]} -eq 0 && -n "${COMFYUI_EXTRA_ARGS:-}" ]]; then
  # The web app only writes allowlisted ComfyUI main.py flags here.
  eval "EXTRA_ARGS=(${COMFYUI_EXTRA_ARGS})"
fi

cd "$COMFYUI_ROOT"
exec "$PYTHON_BIN" main.py --listen "$LISTEN_HOST" --port "$LISTEN_PORT" "${EXTRA_ARGS[@]}"
