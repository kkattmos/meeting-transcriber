#!/bin/bash
# Switch the web UI (and the 15-minute resume loop) on or off under pm2.
#
#   ./webui.sh on        start meeting-bot-web + meeting-bot-resume
#   ./webui.sh off       stop them and remove them from pm2
#   ./webui.sh status    pm2's view of both
#   ./webui.sh url       the address(es) to open, with the token filled in
#   ./webui.sh logs      follow the web UI's log
#
# Off by default, and off again after a reboot: this never runs `pm2 save` or
# `pm2 startup`. Recordings do not need the web UI — a meeting started from
# the command line runs in the background on its own.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"

PM2="$(command -v pm2 || true)"
[ -n "$PM2" ] || [ ! -x "$HOME/.local/bin/pm2" ] || PM2="$HOME/.local/bin/pm2"
if [ -z "$PM2" ]; then
  echo "ERROR: pm2 not found. Install it with:  npm install -g pm2" >&2
  echo "  (./setup.sh does this into ~/.local when npm is present)" >&2
  exit 1
fi
APPS=(meeting-bot-web meeting-bot-resume)

print_urls() {
  local port="${MEETING_BOT_PORT:-8765}" addr host
  IFS=',' read -r -a addrs <<< "${MEETING_BOT_BIND:-127.0.0.1}"
  for addr in "${addrs[@]}"; do
    addr="$(printf '%s' "$addr" | tr -d '[:space:]')"
    case "$addr" in
      ''|127.0.0.1|localhost|0.0.0.0) host="localhost" ;;
      tailscale) host="$(tailscale ip -4 2>/dev/null | head -n 1)"; [ -n "$host" ] || continue ;;
      *) host="$addr" ;;
    esac
    # The token rides in the #fragment: the page stores it, and a fragment is
    # never sent to the server or written to any log.
    echo "    http://${host}:${port}/#token=${MEETING_BOT_TOKEN:-}"
  done
}

case "${1:-status}" in
  on)
    if [ -z "${MEETING_BOT_TOKEN:-}" ]; then
      echo "ERROR: MEETING_BOT_TOKEN is not set in .env (./setup.sh generates one)." >&2
      exit 1
    fi
    "$PM2" start "$SCRIPT_DIR/ecosystem.config.js" >/dev/null
    "$PM2" ls
    echo ""
    echo "Web UI is on. Open:"
    print_urls
    echo "Turn it off with ./webui.sh off  (it will not start again at boot)."
    ;;
  off)
    for app in "${APPS[@]}"; do "$PM2" delete "$app" >/dev/null 2>&1 || true; done
    echo "Web UI and resume loop are off."
    ;;
  status) "$PM2" ls ;;
  url) print_urls ;;
  logs) exec "$PM2" logs meeting-bot-web ;;
  -h|--help) sed -n '2,13p' "$0" ;;
  *) echo "Usage: $0 on|off|status|url|logs" >&2; exit 1 ;;
esac
