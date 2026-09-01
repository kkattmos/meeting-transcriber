#!/bin/bash
# Runs INSIDE the recorder container. Started by first_time_login.sh on the
# Alpine host — don't run this directly on the host, it needs Chrome/Xvfb/x11vnc,
# none of which exist there.
#
# Brings up a viewable browser: Xvfb -> x11vnc -> noVNC (websockify) -> Chrome,
# using the persistent profile that capture.py reuses later. The operator opens
# noVNC from their own machine (SSH tunnel or Tailscale) and signs in.
set -e

DISPLAY_NUM="${DISPLAY_NUM:-99}"
VNC_PORT="${VNC_PORT:-5901}"
NOVNC_PORT="${NOVNC_PORT:-6080}"
SCREENSHOT_INTERVAL="${SCREENSHOT_INTERVAL:-0}"
SCREENSHOT_DIR="${SCREENSHOT_DIR:-/opt/meeting-bot/login-screenshots}"
START_URL="${START_URL:-https://accounts.google.com/}"
PROFILE_DIR="$HOME/.meeting-bot/chrome-profile"

export DISPLAY=":${DISPLAY_NUM}"

CHROME_PID=""
SHOT_PID=""

cleanup() {
  [ -n "$SHOT_PID" ] && kill "$SHOT_PID" 2>/dev/null
  [ -n "$CHROME_PID" ] && kill "$CHROME_PID" 2>/dev/null
  pkill -f "x11vnc -display :${DISPLAY_NUM}" 2>/dev/null
  pkill -f "websockify" 2>/dev/null
  pkill -f "Xvfb :${DISPLAY_NUM}" 2>/dev/null
  true
}
trap cleanup EXIT

echo "==> Starting virtual display :${DISPLAY_NUM} (1920x1080)"
Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24 &
sleep 1

echo "==> Starting x11vnc on ${VNC_PORT}"
# -rfbport pins the port; without it x11vnc auto-picks the first free one and
# silently breaks the fixed websockify target below.
x11vnc -display ":${DISPLAY_NUM}" -rfbport "${VNC_PORT}" -forever -shared -nopw -quiet -bg

echo "==> Starting noVNC bridge on ${NOVNC_PORT}"
websockify --web=/usr/share/novnc/ "${NOVNC_PORT}" "localhost:${VNC_PORT}" &
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
      ffmpeg -y -loglevel error -f x11grab -video_size 1920x1080 \
        -i ":${DISPLAY_NUM}" -frames:v 1 "$SCREENSHOT_DIR/latest.png" 2>/dev/null || true
      sleep "$SCREENSHOT_INTERVAL"
    done
  ) &
  SHOT_PID=$!
fi

echo "==> Launching Google Chrome (profile: $PROFILE_DIR)"
mkdir -p "$PROFILE_DIR"
# The profile dir is bind-mounted from the host and outlives this container,
# but the container itself is ephemeral and gets a new hostname every run
# (first_time_login.sh does `docker rm -f` before each start). Chrome's
# SingletonLock encodes the hostname+pid of whoever last held it, so a lock
# left behind by a previous container (crash, Ctrl+C racing the trap, `docker
# rm -f` on a hung container) makes this container's Chrome refuse to start
# with "profile appears to be in use by another computer". Since this is
# always a fresh container, any lock present here is guaranteed stale.
rm -f "$PROFILE_DIR"/Singleton{Lock,Socket,Cookie}
# Launched DIRECTLY, not through Playwright. Even with channel="chrome",
# Playwright sets --enable-automation and navigator.webdriver=true, which
# Google's sign-in flow rejects with "This browser or app may not be secure".
# See CLAUDE.md — routing this through Playwright is a known-broken "fix".
#
# --no-sandbox: we're root inside the container; sandbox + root = crash.
# NOT --kiosk (unlike the recording path): the operator needs an address bar
# and tabs to get through Google's and Zoom's sign-in flows.
google-chrome-stable \
  --user-data-dir="$PROFILE_DIR" \
  --no-sandbox \
  --no-first-run \
  --no-default-browser-check \
  --disable-gpu \
  --disable-software-rasterizer \
  --disable-dev-shm-usage \
  --disable-features=ScreenCapture \
  --window-position=0,0 \
  --window-size=1920,1080 \
  --lang=th-TH \
  "$START_URL" &
CHROME_PID=$!

echo "==> Chrome is up. Sign into Google and Zoom, then stop this from the host."
wait "$CHROME_PID" || true
