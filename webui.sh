#!/bin/bash
# Switch the web UI (and the 15-minute resume timer) on or off — systemd on
# this server branch; the desktop branch (debian13) does the same with pm2.
#
#   ./webui.sh on        install (if needed) and start meeting-bot-trigger.service
#                        and meeting-bot-resume.timer
#   ./webui.sh off       stop both (and keep them from starting at boot)
#   ./webui.sh restart   after changing the token, port or bind address
#                        (any other .env edit reaches the next run by itself)
#   ./webui.sh status    systemd's view of both
#   ./webui.sh url       the address(es) to open, with the token filled in
#   ./webui.sh logs      follow the web UI's journal
#
# `on` enables the units, so they come back after a reboot — a server has no
# one to switch them on by hand. `off` disables them again.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"
WEB=meeting-bot-trigger.service
TIMER=meeting-bot-resume.timer

need_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: ./webui.sh $1 manages systemd units — run it as root." >&2
    exit 1
  fi
}

install_units() {
  local unit venv="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}"
  for unit in meeting-bot-trigger.service meeting-bot-resume.service meeting-bot-resume.timer; do
    sed -e "s|@REPO_ROOT@|$SCRIPT_DIR|g" -e "s|@VENV@|$venv|g" \
      "$SCRIPT_DIR/$unit" > "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
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

case "${1:-}" in
  on)
    need_root on
    [ -n "${MEETING_BOT_TOKEN:-}" ] || { echo "ERROR: MEETING_BOT_TOKEN is not set in .env (sudo ./setup.sh makes one)." >&2; exit 1; }
    install_units
    systemctl enable --now "$WEB" "$TIMER" >/dev/null
    echo "==> Web UI on. Open:"
    print_urls
    ;;
  off)
    need_root off
    systemctl disable --now "$WEB" "$TIMER" >/dev/null 2>&1 || true
    echo "==> Web UI and the resume timer are off."
    ;;
  restart)
    need_root restart
    install_units
    systemctl restart "$WEB"
    echo "==> Restarted. Open:"
    print_urls
    ;;
  status) systemctl --no-pager status "$WEB" "$TIMER" || true ;;
  url)    print_urls ;;
  logs)   journalctl -u "$WEB" -f ;;
  *)
    sed -n '2,15p' "$0"
    exit 1
    ;;
esac
