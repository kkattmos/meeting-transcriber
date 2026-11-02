#!/bin/bash
# Discord voice spike, candidate 2: discord.js + @discordjs/voice, the
# official library (DAVE receive since 0.19).
#
# It needs Node >= 22.12; Debian 13 ships 20. With --fetch-node this script
# downloads the current Node 22 LTS from nodejs.org into ./node-v22 (checked
# against nodejs.org's SHASUMS256.txt) and uses that — nothing system-wide.
#
#   ./spike/discord/node/run.sh                 install deps if needed, then run
#   ./spike/discord/node/run.sh --fetch-node    first fetch Node 22 beside it
#
# Needs in .env: DISCORD_BOT_TOKEN, DISCORD_SPIKE_GUILD_ID. The mix uses the
# project's .venv (lib/discord_spool.py). Remove everything with:
#   rm -rf spike/discord/node/node_modules spike/discord/node/node-v22
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
SCRIPT_DIR="$REPO"
# shellcheck source=/dev/null
. "$REPO/source_env.sh"

LOCAL_NODE="$HERE/node-v22"

if [ "${1:-}" = "--fetch-node" ]; then
  base="https://nodejs.org/dist/latest-v22.x"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL "$base/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt"
  tarball="$(grep -oE 'node-v22\.[0-9]+\.[0-9]+-linux-x64\.tar\.xz' "$tmp/SHASUMS256.txt" | head -n 1)"
  [ -n "$tarball" ] || { echo "spike: no linux-x64 tarball listed at $base" >&2; exit 1; }
  echo "==> Downloading $tarball"
  curl -fSL "$base/$tarball" -o "$tmp/$tarball"
  (cd "$tmp" && grep " $tarball\$" SHASUMS256.txt | sha256sum -c -)
  rm -rf "$LOCAL_NODE"
  mkdir -p "$LOCAL_NODE"
  tar -xJf "$tmp/$tarball" -C "$LOCAL_NODE" --strip-components=1
fi
[ -x "$LOCAL_NODE/bin/node" ] && export PATH="$LOCAL_NODE/bin:$PATH"

command -v node >/dev/null || { echo "spike: node not found (try --fetch-node)" >&2; exit 1; }
ver="$(node -p 'process.versions.node')"
major="${ver%%.*}"; rest="${ver#*.}"; minor="${rest%%.*}"
if [ "$major" -lt 22 ] || { [ "$major" -eq 22 ] && [ "$minor" -lt 12 ]; }; then
  echo "spike: Node $ver is too old; @discordjs/voice 0.19 needs >= 22.12." >&2
  echo "  Re-run with --fetch-node (downloads Node 22 into $LOCAL_NODE only)." >&2
  exit 1
fi
[ -x "${MEETING_BOT_VENV:-$REPO/.venv}/bin/python3" ] \
  || { echo "spike: the project .venv is missing (./setup.sh builds it)" >&2; exit 1; }

cd "$HERE"
[ -d node_modules ] || npm install --no-audit --no-fund
echo "==> Starting the spike bot on Node $ver (Ctrl+C to stop). In Discord: /spike_join, talk, /spike_stop"
exec node spike_recv.mjs
