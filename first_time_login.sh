#!/bin/bash
# Run this ONCE after setup.sh, and again whenever your Google/Zoom session
# expires. It opens a real, viewable Google Chrome using the SAME persistent
# profile the recorder reuses later, so the bot joins meetings already signed in.
#
# The Alpine host has no display and (in Proxmox) no window you can see, so
# Chrome runs on a headless Xvfb inside the recorder container and is exposed
# to YOU over noVNC — a plain web page you open from your own machine.
#
#   ./first_time_login.sh                     bind noVNC to localhost, print an ssh -L command
#   ./first_time_login.sh --tailscale         bind to this host's Tailscale IP instead
#   ./first_time_login.sh --bind 0.0.0.0      bind to every interface (LAN-visible; see warning)
#   ./first_time_login.sh --screenshot        also dump the display to a PNG every 10s
#   ./first_time_login.sh --url <url>         open somewhere other than accounts.google.com
#
# Chrome lives in the container because Google ships no musl build and
# unbranded Chromium gets blocked by the sign-in flow. See CLAUDE.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"
export REPO_ROOT

# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/docker/recorder_lib.sh"

BIND_ADDR="127.0.0.1"
BIND_MODE="localhost"
NOVNC_PORT="${NOVNC_PORT:-6080}"
SCREENSHOT_INTERVAL=0
START_URL="https://accounts.google.com/"
CONTAINER_NAME="${LOGIN_CONTAINER_NAME:-meeting-bot-login}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tailscale)
      TS_IP="$(recorder_tailscale_ip)"
      if [ -z "$TS_IP" ]; then
        echo "ERROR: --tailscale given but no Tailscale IPv4 found." >&2
        echo "  Is tailscaled running on this host?  tailscale status" >&2
        exit 1
      fi
      BIND_ADDR="$TS_IP"
      BIND_MODE="tailscale"
      shift
      ;;
    --bind)
      [ -n "${2:-}" ] || { echo "--bind needs an address" >&2; exit 1; }
      BIND_ADDR="$2"; BIND_MODE="custom"; shift 2
      ;;
    --port)
      [ -n "${2:-}" ] || { echo "--port needs a number" >&2; exit 1; }
      NOVNC_PORT="$2"; shift 2
      ;;
    --screenshot)
      SCREENSHOT_INTERVAL=10; shift
      ;;
    --screenshot-interval)
      [ -n "${2:-}" ] || { echo "--screenshot-interval needs a number" >&2; exit 1; }
      SCREENSHOT_INTERVAL="$2"; shift 2
      ;;
    --url)
      [ -n "${2:-}" ] || { echo "--url needs a value" >&2; exit 1; }
      START_URL="$2"; shift 2
      ;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0
      ;;
    *)
      echo "Unknown argument: $1 (try --help)" >&2; exit 1
      ;;
  esac
done

recorder_require_docker
recorder_require_image

# A leftover container from a previous run holds the port and the profile lock.
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

cleanup() {
  echo ""
  echo "==> Stopping the login container"
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "==> Starting Chrome + noVNC in the recorder container"
recorder_run "$CONTAINER_NAME" detach \
  -e "SCREENSHOT_INTERVAL=$SCREENSHOT_INTERVAL" \
  -e "START_URL=$START_URL" \
  -p "$BIND_ADDR:$NOVNC_PORT:6080" \
  -- bash /app/docker/login_entry.sh >/dev/null

# Give the stack (Xvfb -> x11vnc -> websockify -> Chrome) a moment, then confirm
# it actually came up. Failing here beats the operator staring at a browser tab
# that never connects — they can't see the console to find out why.
for _ in $(seq 1 20); do
  if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "ERROR: the login container exited during startup. Last 40 log lines:" >&2
    docker logs "$CONTAINER_NAME" 2>&1 | tail -n 40 >&2 || true
    exit 1
  fi
  if docker exec "$CONTAINER_NAME" pgrep -f google-chrome >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo ""
echo "=================================================================="
echo "  Open the browser from YOUR OWN machine"
echo "=================================================================="
case "$BIND_MODE" in
  localhost)
    echo "noVNC is bound to 127.0.0.1:$NOVNC_PORT here, so nothing is exposed to"
    echo "the network. Run this on your laptop:"
    echo ""
    echo "    ssh -L ${NOVNC_PORT}:localhost:${NOVNC_PORT} $(id -un)@$(hostname)"
    echo ""
    echo "then open:"
    echo "    http://localhost:${NOVNC_PORT}/vnc.html"
    ;;
  tailscale)
    echo "noVNC is bound to this host's Tailscale address. From any machine on"
    echo "your tailnet, open:"
    echo ""
    echo "    http://${BIND_ADDR}:${NOVNC_PORT}/vnc.html"
    ;;
  *)
    # BIND_ADDR may be 0.0.0.0 (a bind-any wildcard) or a literal IP. Pick a
    # routable URL the operator can actually paste — 0.0.0.0 isn't a valid
    # destination, so fall back to this host's primary IPv4.
    DISPLAY_HOST="$BIND_ADDR"
    if [ "$DISPLAY_HOST" = "0.0.0.0" ] || [ -z "$DISPLAY_HOST" ]; then
      DISPLAY_HOST="$(recorder_host_ipv4 2>/dev/null || hostname -i 2>/dev/null | awk '{print $1}')"
    fi
    echo "noVNC is bound to ${BIND_ADDR}:${NOVNC_PORT} on this host. Open:"
    echo ""
    echo "    http://${DISPLAY_HOST}:${NOVNC_PORT}/vnc.html"
    echo ""
    echo "WARNING: this VNC session has no password and hands whoever reaches it"
    echo "full control of a browser holding your Google session. Only do this on"
    echo "a trusted network, and stop the script as soon as you're signed in."
    ;;
esac
echo ""
echo "Click Connect, sign into Google, then open zoom.us in the same window and"
echo "sign in there too. Both sessions land in the shared profile at:"
echo "    $CHROME_PROFILE_DIR"
if [ "$SCREENSHOT_INTERVAL" -gt 0 ]; then
  echo ""
  echo "Screenshot mode is on — if noVNC won't reach, check what's on screen with:"
  echo "    scp $(id -un)@$(hostname):$MEETING_BOT_ROOT/login-screenshots/latest.png ."
fi
echo ""
echo "Press Ctrl+C here when you're done."
echo "=================================================================="
echo ""

# Stream the container's output so a crash is visible rather than silent, and
# block until the operator interrupts (or Chrome exits on its own).
docker logs -f "$CONTAINER_NAME" 2>&1 || true
