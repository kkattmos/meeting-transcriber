#!/bin/bash
# Run this ONCE after setup.sh, and again whenever your Google/Zoom session
# expires. It opens a real, viewable Google Chrome using the SAME persistent
# profile the recorder reuses later, so the bot joins meetings already signed in.
#
# The box has no monitor, so Chrome runs on a headless Xvfb here and is exposed
# to YOU over noVNC — a plain web page you open from your own machine.
#
#   ./first_time_login.sh                     bind noVNC to localhost, print an ssh -L command
#   ./first_time_login.sh --tailscale         bind to this host's Tailscale IP instead
#   ./first_time_login.sh --bind 0.0.0.0      bind to every interface (LAN-visible; see warning)
#   ./first_time_login.sh --screenshot        also dump the display to a PNG every 10s
#   ./first_time_login.sh --url <url>         open somewhere other than accounts.google.com
#
# Chrome is launched DIRECTLY here, not through Playwright: even with
# channel="chrome", Playwright sets --enable-automation and
# navigator.webdriver=true, which Google's sign-in flow rejects with "This
# browser or app may not be secure". See CLAUDE.md — routing this through
# Playwright is a known-broken "fix".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib/paths.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib/xsession.sh"

BIND_ADDR="127.0.0.1"
BIND_MODE="localhost"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5901}"
SCREENSHOT_INTERVAL=0
START_URL="https://accounts.google.com/"
GEOMETRY="${RECORD_GEOMETRY:-1920x1080}"
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-$MEETING_BOT_ROOT/chrome-profile}"
SCREENSHOT_DIR="${SCREENSHOT_DIR:-$MEETING_BOT_ROOT/login-screenshots}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tailscale)
      TS_IP="$(xsession_tailscale_ip)"
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
      sed -n '2,20p' "$0"; exit 0
      ;;
    *)
      echo "Unknown argument: $1 (try --help)" >&2; exit 1
      ;;
  esac
done

xsession_require_tools Xvfb x11vnc websockify google-chrome-stable || exit 1

NOVNC_WEB=""
for candidate in /usr/share/novnc /usr/share/webapps/novnc; do
  [ -d "$candidate" ] && NOVNC_WEB="$candidate" && break
done
if [ -z "$NOVNC_WEB" ]; then
  echo "ERROR: noVNC's web assets were not found (looked in /usr/share/novnc)." >&2
  echo "  Install them with:  sudo apt-get install novnc" >&2
  exit 1
fi

mkdir -p "$CHROME_PROFILE_DIR"

CHROME_PID=""
SHOT_PID=""
WEBSOCKIFY_PID=""

cleanup() {
  # `|| true` everywhere: EXIT trap under `set -e`, and every one of these can
  # legitimately fail (already-dead pid, nothing matching pkill).
  echo ""
  echo "==> Shutting the login session down"
  [ -n "$SHOT_PID" ] && kill "$SHOT_PID" 2>/dev/null || true
  [ -n "$CHROME_PID" ] && kill "$CHROME_PID" 2>/dev/null || true
  [ -n "$WEBSOCKIFY_PID" ] && kill "$WEBSOCKIFY_PID" 2>/dev/null || true
  [ -n "${XVFB_DISPLAY_NUM:-}" ] && \
    pkill -f "x11vnc -display :${XVFB_DISPLAY_NUM}" 2>/dev/null || true
  xsession_stop_xvfb || true
  true
}
trap cleanup EXIT INT TERM

DISPLAY_NUM="$(xsession_pick_display)" || exit 1
echo "==> Starting virtual display :$DISPLAY_NUM ($GEOMETRY)"
xsession_start_xvfb "$DISPLAY_NUM" "$GEOMETRY" || exit 1

echo "==> Starting x11vnc on $VNC_PORT"
# -rfbport pins the port; without it x11vnc auto-picks the first free one and
# silently breaks the fixed websockify target below.
x11vnc -display ":$DISPLAY_NUM" -rfbport "$VNC_PORT" -forever -shared -nopw \
       -quiet -bg >/dev/null

echo "==> Starting the noVNC bridge on ${BIND_ADDR}:${NOVNC_PORT}"
websockify --web="$NOVNC_WEB" "${BIND_ADDR}:${NOVNC_PORT}" \
           "localhost:${VNC_PORT}" >/dev/null 2>&1 &
WEBSOCKIFY_PID=$!
sleep 1

# Optional blind-diagnostics: dump the display to a PNG every N seconds so the
# operator can confirm what's on screen over plain SSH when noVNC won't reach.
# Off by default — a login screen is exactly the thing you don't want
# accidentally persisted to disk.
if [ "$SCREENSHOT_INTERVAL" -gt 0 ] 2>/dev/null; then
  mkdir -p "$SCREENSHOT_DIR"
  echo "==> Screenshot mode: writing $SCREENSHOT_DIR/latest.png every ${SCREENSHOT_INTERVAL}s"
  (
    while true; do
      ffmpeg -y -loglevel error -f x11grab -video_size "$GEOMETRY" \
        -i ":$DISPLAY_NUM" -frames:v 1 "$SCREENSHOT_DIR/latest.png" 2>/dev/null || true
      sleep "$SCREENSHOT_INTERVAL"
    done
  ) &
  SHOT_PID=$!
fi

echo "==> Launching Google Chrome (profile: $CHROME_PROFILE_DIR)"
# Chrome's SingletonLock encodes the hostname+pid of whoever last held it, so
# a lock left behind by a killed session (Ctrl+C racing the trap, a reboot)
# makes this Chrome refuse to start with "profile appears to be in use by
# another computer". Nothing else is using it at this point: this script owns
# the profile for as long as it runs, and the recorder never runs concurrently
# with a login session.
rm -f "$CHROME_PROFILE_DIR"/Singleton{Lock,Socket,Cookie}
# --no-sandbox: this runs as root. Sandbox + root = crash on launch.
# NOT --kiosk (unlike the recording path): the operator needs an address bar
# and tabs to get through Google's and Zoom's sign-in flows.
google-chrome-stable \
  --user-data-dir="$CHROME_PROFILE_DIR" \
  --no-sandbox \
  --no-first-run \
  --no-default-browser-check \
  --disable-gpu \
  --disable-software-rasterizer \
  --disable-dev-shm-usage \
  --disable-features=ScreenCapture \
  --window-position=0,0 \
  --window-size="${GEOMETRY/x/,}" \
  --lang=th-TH \
  "$START_URL" >/dev/null 2>&1 &
CHROME_PID=$!

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
      DISPLAY_HOST="$(xsession_host_ipv4 2>/dev/null || hostname -i 2>/dev/null | awk '{print $1}')"
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
  echo "    scp $(id -un)@$(hostname):$SCREENSHOT_DIR/latest.png ."
fi
echo ""
echo "Press Ctrl+C here when you're done."
echo "=================================================================="
echo ""

# Block until the operator interrupts (or Chrome exits on its own).
wait "$CHROME_PID" || true
