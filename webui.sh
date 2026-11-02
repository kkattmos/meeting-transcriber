#!/bin/bash
# The web UI (an OpenRC service) and the 15-minute resume (a crond entry) on
# the Alpine host — the `main` branch's form of debian13's pm2 ./webui.sh.
#
#   ./webui.sh on        install + start both, and enable them at boot
#   ./webui.sh off       stop both and disable them
#   ./webui.sh restart   after changing the token, port or bind address
#   ./webui.sh status | url | logs
#
# (`on --web-only` / `on --resume-only` are what setup.sh's flags use.)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"
SVC=meeting-bot-web
CRON=/etc/crontabs/root
MARK="# meeting-bot resume"

need_root() { [ "$(id -u)" -eq 0 ] || { echo "ERROR: ./webui.sh $1 needs root." >&2; exit 1; }; }

web_on() {
  sed "s|@REPO_ROOT@|$SCRIPT_DIR|g" "$SCRIPT_DIR/openrc/$SVC" > "/etc/init.d/$SVC"
  chmod 755 "/etc/init.d/$SVC"
  rc-update add "$SVC" default >/dev/null
  rc-service "$SVC" restart >/dev/null 2>&1 || rc-service "$SVC" start
}
resume_on() {
  mkdir -p "$(dirname "$CRON")" "$MEETING_BOT_ROOT/logs"
  touch "$CRON"
  grep -vF "$MARK" "$CRON" > "$CRON.tmp" || true
  echo "*/15 * * * * MEETING_BOT_FOREGROUND=1 /bin/bash $SCRIPT_DIR/pipeline.sh --resume-all >> $MEETING_BOT_ROOT/logs/resume.log 2>&1 $MARK" >> "$CRON.tmp"
  mv "$CRON.tmp" "$CRON"
  rc-update add crond default >/dev/null 2>&1 || true
  rc-service crond restart >/dev/null 2>&1 || rc-service crond start >/dev/null 2>&1 || true
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
    echo "    http://${host}:${port}/#token=${MEETING_BOT_TOKEN:-}"
  done
}

case "${1:-}" in
  on)
    need_root on
    case "${2:-}" in
      --web-only) web_on ;;
      --resume-only) resume_on ;;
      *) web_on; resume_on ;;
    esac
    echo "==> On. Open:"; print_urls ;;
  off)
    need_root off
    rc-service "$SVC" stop >/dev/null 2>&1 || true
    rc-update del "$SVC" default >/dev/null 2>&1 || true
    if [ -f "$CRON" ]; then grep -vF "$MARK" "$CRON" > "$CRON.tmp" || true; mv "$CRON.tmp" "$CRON"; fi
    echo "==> Off." ;;
  restart) need_root restart; web_on; echo "==> Restarted."; print_urls ;;
  status) rc-service "$SVC" status || true; grep -F "$MARK" "$CRON" 2>/dev/null || echo "  (resume cron off)" ;;
  url) print_urls ;;
  logs) tail -f /var/log/meeting-bot-web.log ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
