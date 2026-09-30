#!/bin/bash
# Discord voice spike, candidate 1: discord.py (zacker150's fork) +
# discord-ext-voice-recv, the only Python receive stack that decrypts DAVE.
#
# Builds a THROWAWAY venv beside this script (never the project's .venv — the
# fork of discord.py must not leak into the pipeline's dependencies), then
# runs the spike bot in the foreground. Ctrl+C stops it.
#
#   ./spike/discord/py/run.sh            install if needed, then run
#   ./spike/discord/py/run.sh --reinstall
#
# Needs in .env: DISCORD_BOT_TOKEN, DISCORD_SPIKE_GUILD_ID (see README.md,
# "Discord voice bot"). Remove everything with: rm -rf spike/discord/py/.venv
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
SCRIPT_DIR="$REPO"
# shellcheck source=/dev/null
. "$REPO/source_env.sh"

# The commit read and checked on 2026-09-30 (DAVE receive, #16-#19). It pins
# its own discord.py fork in its package metadata.
VOICE_RECV="git+https://github.com/zacker150/discord-ext-voice-recv@dcf543a"
VENV="$HERE/.venv"

if ! ldconfig -p 2>/dev/null | grep -q 'libopus\.so'; then
  echo "spike: libopus is not installed (discord.py decodes Opus through it):" >&2
  echo "  sudo apt install libopus0" >&2
  exit 1
fi
command -v uv >/dev/null || { echo "spike: uv not found (./setup.sh installs it)" >&2; exit 1; }
command -v git >/dev/null || { echo "spike: git not found" >&2; exit 1; }

if [ "${1:-}" = "--reinstall" ]; then rm -rf "$VENV"; fi
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> Building the spike venv in $VENV"
  uv venv --python-preference only-system --python 3.13 "$VENV"
  # numpy for lib/discord_spool.py's mixer, same as the project pins.
  uv pip install --python "$VENV/bin/python" "$VOICE_RECV" numpy
fi

echo "==> Starting the spike bot (Ctrl+C to stop). In Discord: /spike_join, talk, /spike_stop"
exec "$VENV/bin/python" -u "$HERE/spike_recv.py"
