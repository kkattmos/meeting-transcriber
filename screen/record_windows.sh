#!/bin/bash
# The Windows recorder's entry point (MEETING_BOT_RECORDER in .env.example):
# the same arguments as screen/record_screen.sh, handed to record_windows.py.
# See that file for how a meeting is recorded on Windows.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$(dirname "$SCRIPT_DIR")/source_env.sh"
exec "$MEETING_BOT_VENV/bin/python3" -u "$SCRIPT_DIR/record_windows.py" "$@"
