#!/bin/bash
# Switch the web UI (and the 15-minute resume loop) on or off under pm2.
#
#   ./webui.sh on        start meeting-bot-web + meeting-bot-resume
#   ./webui.sh off       stop them and remove them from pm2
#   ./webui.sh restart   off + on — use this after editing .env (see below)
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

# pm2 snapshots the environment of the `pm2 start` call and replays it on
# every restart. This script has sourced .env (for the token and the bind
# address), so a plain start would freeze every .env value into pm2 — and
# web/serve.sh, which fills in only variables that are unset, would never see
# a later .env edit, not even after `pm2 restart`. Found 2026-09-29: the web
# UI kept PDF_FONT_SIZE=8 and SUMMARY_PROMPT=lecture-claude after .env said
# otherwise, and every run it started inherited them. So pm2 is started with
# the .env keys removed, and serve.sh reads .env afresh on every (re)start.
# An app pm2 already knows keeps its old snapshot, hence delete-then-start.
start_clean() {
  local key
  for app in "${APPS[@]}"; do "$PM2" delete "$app" >/dev/null 2>&1 || true; done
  (
    if [ -f "$SCRIPT_DIR/.env" ]; then
      while IFS= read -r key; do
        unset "$key" 2>/dev/null || true
      done < <(sed -nE 's/^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=.*/\2/p' \
                 "$SCRIPT_DIR/.env")
    fi
    exec "$PM2" start "$SCRIPT_DIR/ecosystem.config.js" >/dev/null
  )
}

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
    start_clean
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
  restart) exec "$0" on ;;
  status) "$PM2" ls ;;
  url) print_urls ;;
  logs) exec "$PM2" logs meeting-bot-web ;;
  -h|--help) sed -n '2,14p' "$0" ;;
  *) echo "Usage: $0 on|off|restart|status|url|logs" >&2; exit 1 ;;
esac
