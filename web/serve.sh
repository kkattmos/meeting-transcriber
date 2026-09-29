#!/bin/bash
# pm2's entry point for the web UI (ecosystem.config.js). trigger_server.py
# reads its token, port and bind addresses from the environment, and .env is
# where the operator keeps them — so load it the same way every other entry
# script does, then hand over to the venv's Python.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck disable=SC1091
. "$ROOT_DIR/source_env.sh"
PY="${MEETING_BOT_VENV:-$ROOT_DIR/.venv}/bin/python3"
[ -x "$PY" ] || PY="python3"
cd "$ROOT_DIR"
exec "$PY" "$ROOT_DIR/trigger_server.py"
