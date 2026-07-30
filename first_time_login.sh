#!/bin/bash
# Run this ONCE after setup.sh, and again any time your Google/Zoom session expires.
# It opens a real, viewable browser (via noVNC in your web browser) using the SAME
# persistent Chromium profile that the automated join script will reuse later.
# Log into your Google account and your Zoom account in that window, then close this script.
#
# IMPORTANT: run this with sudo (matching how setup.sh was run), e.g.:
#   sudo -H ./first_time_login.sh
# setup.sh installed the venv, Chromium, and cached everything under /root - running
# this script as a different user (or without -H) points Playwright at a different,
# empty home directory and it won't find the browser it needs.

DISPLAY_NUM=99
VNC_PORT=5901
export DISPLAY=:${DISPLAY_NUM}

if [ "$EUID" -ne 0 ]; then
  echo "This must be run as root (matching setup.sh) - try: sudo -H $0"
  exit 1
fi

CHROMIUM_GLOB="$HOME/.cache/ms-playwright/chromium-*/chrome-linux64/chrome"
if ! compgen -G "$CHROMIUM_GLOB" > /dev/null; then
  echo "Chromium isn't installed under \$HOME=$HOME."
  echo "Run this first:  /opt/meeting-bot-venv/bin/playwright install --with-deps chromium"
  echo "(with the same user/sudo context you're using to run this script)"
  exit 1
fi

# We need the real Google Chrome for this one-time login. Playwright would
# launch it with --enable-automation and expose navigator.webdriver=true,
# which Google uses to refuse the sign-in flow ("This browser or app may
# not be secure") even when channel="chrome". Zoom doesn't check, which
# is why Zoom logs in fine but Google doesn't.
if ! command -v google-chrome >/dev/null 2>&1 && ! command -v google-chrome-stable >/dev/null 2>&1; then
  echo "google-chrome isn't installed. Run setup.sh first (it installs google-chrome-stable)."
  exit 1
fi

XVFB_PID=""
NOVNC_PID=""
CHROME_PID=""

cleanup() {
  echo "==> Cleaning up VNC/Xvfb/Chrome"
  [ -n "$CHROME_PID" ] && kill "$CHROME_PID" 2>/dev/null
  pkill -f "google-chrome.*--user-data-dir=.*meeting-bot" 2>/dev/null
  [ -n "$NOVNC_PID" ] && kill "$NOVNC_PID" 2>/dev/null
  pkill -f "x11vnc -display :${DISPLAY_NUM}" 2>/dev/null
  fuser -k "${VNC_PORT}/tcp" 2>/dev/null
  [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null
  rm -f "/tmp/.X${DISPLAY_NUM}-lock"
}
# Runs on normal exit AND on error (no more silently-orphaned background processes)
trap cleanup EXIT

# Clear anything left over from a previous crashed run before starting fresh
pkill -f "Xvfb :${DISPLAY_NUM}" 2>/dev/null
pkill -f "x11vnc -display :${DISPLAY_NUM}" 2>/dev/null
pkill -f "websockify --web=/usr/share/novnc/ 6080" 2>/dev/null
fuser -k "${VNC_PORT}/tcp" 2>/dev/null
fuser -k 6080/tcp 2>/dev/null
rm -f "/tmp/.X${DISPLAY_NUM}-lock"
sleep 1

set -e

echo "==> Starting virtual display :${DISPLAY_NUM}"
# 1920x1080 matches the default Proxmox console; Xvfb head, Chrome's --kiosk
# window, and ffmpeg's -video_size all use this geometry so there are no black
# edges in the recording.
Xvfb :${DISPLAY_NUM} -screen 0 1920x1080x24 &
XVFB_PID=$!
sleep 1

echo "==> Starting VNC server on fixed port ${VNC_PORT}"
# -rfbport pins the port explicitly - without it, x11vnc auto-picks the first
# free port (5901, 5902, 5903...) which silently breaks the fixed websockify
# target below if anything else is occupying lower ports.
x11vnc -display :${DISPLAY_NUM} -rfbport ${VNC_PORT} -forever -nopw -quiet -bg

echo "==> Starting noVNC web bridge on port 6080"
websockify --web=/usr/share/novnc/ 6080 localhost:${VNC_PORT} &
NOVNC_PID=$!

echo ""
echo "=============================================="
echo "Open this in a browser ON YOUR OWN MACHINE:"
echo "  http://<vm-ip>:6080/vnc.html"
echo "(If accessing remotely, tunnel it first, e.g.:"
echo "  ssh -L 6080:localhost:6080 user@<vm-ip>  )"
echo "=============================================="
echo ""

source /opt/meeting-bot-venv/bin/activate
CHROME_BIN="$(command -v google-chrome || command -v google-chrome-stable)"
# Launch real Chrome directly, NOT via Playwright. The persistent --user-data-dir
# writes session cookies into the same profile directory that join_meeting.py
# will reuse. No automation flags, no navigator.webdriver, so Google treats
# this as a normal user browser and the sign-in flow goes through.
# --no-sandbox is required because this script runs as root (matches setup.sh).
"$CHROME_BIN" \
  --user-data-dir="$HOME/.meeting-bot/chrome-profile" \
  --no-sandbox \
  --no-first-run \
  --no-default-browser-check \
  --kiosk \
  --window-size=1920,1080 \
  about:blank &
CHROME_PID=$!
echo "==> Chrome launched (PID $CHROME_PID). Sign in to Google and Zoom in the VNC window."
echo "==> Press Ctrl+C here when done."
wait "$CHROME_PID" || true
deactivate
# cleanup() runs automatically via the trap above, even if the block above fails
