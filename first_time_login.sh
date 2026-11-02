#!/bin/bash
# Run this ONCE after setup.sh, and again whenever the bot's Google/Zoom
# session expires. It opens the recorder's browser (MEETING_BROWSER: Firefox
# ESR by default, or Chrome) on the SAME persistent profile the recorder
# reuses later, so the bot joins meetings already signed in.
#
# The bot signs in as ONE Google account: BOT_GOOGLE_ACCOUNT in .env. The
# sign-in page opens with that address already filled in, and when the window
# closes the profile is checked — signed in as anyone else, this exits 1, and
# the recorder refuses to join a Google Meet from it (screen/browser.py).
#
#   ./first_time_login.sh                     a normal window on this desktop
#   ./first_time_login.sh --novnc             headless Xvfb + noVNC on localhost
#   ./first_time_login.sh --tailscale         noVNC on this host's Tailscale IP
#   ./first_time_login.sh --bind 0.0.0.0      noVNC on every interface (see warning)
#   ./first_time_login.sh --screenshot        (noVNC) dump the display to a PNG every 10s
#   ./first_time_login.sh --url <url>         open somewhere else (e.g. https://zoom.us/signin)
#   ./first_time_login.sh --check             only check which account the profile holds
#
# The browser is launched DIRECTLY here, never through Selenium/Playwright:
# a driven browser sets navigator.webdriver=true, which Google's sign-in flow
# rejects with "This browser or app may not be secure". See CLAUDE.md —
# routing this through the automation driver is a known-broken "fix".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib/paths.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib/xsession.sh"

PY="${MEETING_BOT_VENV:-$LOADER_DIR/.venv}/bin/python3"
[ -x "$PY" ] || PY="python3"

# A desktop session has a display to put the window on; a headless box (or
# an SSH login) gets the noVNC path.
MODE="novnc"
if [ -n "${WAYLAND_DISPLAY:-}" ] || [ -n "${DISPLAY:-}" ]; then
  MODE="desktop"
fi
BIND_ADDR="127.0.0.1"
BIND_MODE="localhost"
NOVNC_PORT="${NOVNC_PORT:-6080}"
VNC_PORT="${VNC_PORT:-5901}"
SCREENSHOT_INTERVAL=0
START_URL=""
CHECK_ONLY=0
GEOMETRY="${RECORD_GEOMETRY:-1920x1080}"
SCREENSHOT_DIR="${SCREENSHOT_DIR:-$MEETING_BOT_ROOT/login-screenshots}"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --novnc) MODE="novnc"; shift ;;
    --desktop) MODE="desktop"; shift ;;
    --tailscale)
      TS_IP="$(xsession_tailscale_ip)"
      if [ -z "$TS_IP" ]; then
        echo "ERROR: --tailscale given but no Tailscale IPv4 found." >&2
        echo "  Is tailscaled running on this host?  tailscale status" >&2
        exit 1
      fi
      BIND_ADDR="$TS_IP"; BIND_MODE="tailscale"; MODE="novnc"
      shift
      ;;
    --bind)
      [ -n "${2:-}" ] || { echo "--bind needs an address" >&2; exit 1; }
      BIND_ADDR="$2"; BIND_MODE="custom"; MODE="novnc"; shift 2
      ;;
    --port)
      [ -n "${2:-}" ] || { echo "--port needs a number" >&2; exit 1; }
      NOVNC_PORT="$2"; shift 2
      ;;
    --screenshot) SCREENSHOT_INTERVAL=10; shift ;;
    --screenshot-interval)
      [ -n "${2:-}" ] || { echo "--screenshot-interval needs a number" >&2; exit 1; }
      SCREENSHOT_INTERVAL="$2"; shift 2
      ;;
    --url)
      [ -n "${2:-}" ] || { echo "--url needs a value" >&2; exit 1; }
      START_URL="$2"; shift 2
      ;;
    --check) CHECK_ONLY=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1 (try --help)" >&2; exit 1 ;;
  esac
done

if [ -z "${BOT_GOOGLE_ACCOUNT:-}" ]; then
  echo "ERROR: BOT_GOOGLE_ACCOUNT is not set." >&2
  echo "  Put the bot's Google address in .env, e.g." >&2
  echo "      BOT_GOOGLE_ACCOUNT=the-bot-account@gmail.com" >&2
  echo "  The login page is prefilled with it, and the recorder refuses to join" >&2
  echo "  a Google Meet from a profile signed in as anyone else." >&2
  exit 1
fi

check_account() {
  echo ""
  echo "==> Checking which Google account the profile is signed in as"
  if "$PY" "$SCRIPT_DIR/screen/browser.py" check-account; then
    return 0
  fi
  echo "" >&2
  echo "The recorder will refuse to join Google Meet until the profile is signed" >&2
  echo "in as $BOT_GOOGLE_ACCOUNT. Run this script again, sign OUT of any other" >&2
  echo "account in that window, and sign in as $BOT_GOOGLE_ACCOUNT." >&2
  return 1
}

if [ "$CHECK_ONLY" -eq 1 ]; then
  check_account
  exit $?
fi

eval "$("$PY" "$SCRIPT_DIR/screen/browser.py" info \
        | awk '/^browser:/ {print "BROWSER_KIND=" $2}
               /^binary:/  {print "BROWSER_BIN=" $2}
               /^profile:/ {sub(/^profile: /, ""); print "PROFILE_DIR=\"" $0 "\""}')"
if [ -z "${BROWSER_BIN:-}" ] || [ "$BROWSER_BIN" = "(not" ]; then
  echo "ERROR: the $BROWSER_KIND binary was not found." >&2
  echo "  Firefox ESR: sudo apt-get install firefox-esr   (or set FIREFOX_BIN)" >&2
  echo "  Chrome:      sudo ./setup.sh --system --with-chrome" >&2
  exit 1
fi

if [ -z "$START_URL" ]; then
  EMAIL_Q="$("$PY" -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1]))' \
             "$BOT_GOOGLE_ACCOUNT")"
  START_URL="https://accounts.google.com/ServiceLogin?Email=${EMAIL_Q}&continue=https%3A%2F%2Fmeet.google.com%2F"
fi

mkdir -p "$PROFILE_DIR"

# A browser still holding the profile — a recording in progress — must not
# be fought over. A lock whose pid is gone is stale (a killed run) and goes.
profile_lock_pid() {
  local link
  for name in lock SingletonLock; do
    link="$(readlink "$PROFILE_DIR/$name" 2>/dev/null)" || continue
    echo "${link##*[-+]}"
    return 0
  done
  return 1
}
if LOCK_PID="$(profile_lock_pid)" && [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
  echo "ERROR: the profile is in use by pid $LOCK_PID (a recording?)." >&2
  echo "  Let it finish, or stop it with ./kill_meeting.sh, then run this again." >&2
  exit 1
fi
rm -f "$PROFILE_DIR"/{lock,.parentlock} "$PROFILE_DIR"/Singleton{Lock,Socket,Cookie}
# geckodriver writes its automation prefs into user.js on every recording and
# they would apply to this window too. The recorder rewrites them next time.
[ -f "$PROFILE_DIR/user.js" ] && mv -f "$PROFILE_DIR/user.js" "$PROFILE_DIR/user.js.recorder"

# The login window: an address bar and tabs (NOT --kiosk), because Google's
# and Zoom's sign-in flows need them.
launch_login_browser() {
  if [ "$BROWSER_KIND" = "chrome" ]; then
    local -a extra=()
    # Chrome refuses to run as root with its sandbox; as a user it keeps it.
    [ "$(id -u)" -eq 0 ] && extra+=(--no-sandbox)
    "$BROWSER_BIN" \
      --user-data-dir="$PROFILE_DIR" \
      "${extra[@]}" \
      --no-first-run \
      --no-default-browser-check \
      --disable-features=ScreenCapture \
      --window-position=0,0 \
      --window-size="${GEOMETRY/x/,}" \
      --lang=th-TH \
      "$START_URL" >/dev/null 2>&1 &
  else
    # --no-remote + --new-instance: a separate Firefox from any the operator
    # already has open, on the bot's profile only. Without them the URL would
    # open as a tab in the operator's own Firefox and own Google session.
    "$BROWSER_BIN" --no-remote --new-instance --profile "$PROFILE_DIR" \
      --width "${GEOMETRY%x*}" --height "${GEOMETRY#*x}" \
      "$START_URL" >/dev/null 2>&1 &
  fi
  BROWSER_PID=$!
}

BROWSER_PID=""
SHOT_PID=""
WEBSOCKIFY_PID=""

if [ "$MODE" = "desktop" ]; then
  echo "==> Opening $BROWSER_KIND on your desktop (profile: $PROFILE_DIR)"
  launch_login_browser
  echo ""
  echo "=================================================================="
  echo "  Sign in as: $BOT_GOOGLE_ACCOUNT"
  echo "=================================================================="
  echo "The address is already filled in on Google's page. Sign in ONLY as that"
  echo "account — if another account appears in this window, sign it out."
  echo "Then open zoom.us in the same window and sign in there too, if you"
  echo "record Zoom calls."
  echo ""
  echo "Close the window when you're done; the account is checked afterwards."
  echo "=================================================================="
  wait "$BROWSER_PID" || true
  check_account
  exit $?
fi

# --- noVNC: a headless box, or a remote operator -----------------------------

xsession_require_tools Xvfb x11vnc websockify || exit 1

NOVNC_WEB=""
for candidate in /usr/share/novnc /usr/share/webapps/novnc; do
  [ -d "$candidate" ] && NOVNC_WEB="$candidate" && break
done
if [ -z "$NOVNC_WEB" ]; then
  echo "ERROR: noVNC's web assets were not found (looked in /usr/share/novnc)." >&2
  echo "  Install them with:  sudo apt-get install novnc" >&2
  exit 1
fi

CLEANED=0
cleanup() {
  # Trapped on INT/TERM *and* EXIT, so a Ctrl+C runs this twice: once for the
  # signal, once as the shell exits. Everything below is idempotent, but the
  # second pass reprints the banner and — if another run had already claimed
  # the display number we just released — would stop that one's Xvfb instead
  # of ours.
  [ "$CLEANED" -eq 1 ] && return 0
  CLEANED=1
  # `|| true` everywhere: EXIT trap under `set -e`, and every one of these can
  # legitimately fail (already-dead pid, nothing matching pkill).
  echo ""
  echo "==> Shutting the login session down"
  [ -n "$SHOT_PID" ] && kill "$SHOT_PID" 2>/dev/null || true
  [ -n "$BROWSER_PID" ] && kill "$BROWSER_PID" 2>/dev/null || true
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
# The window belongs on the Xvfb head, not on a desktop this may also run in
# (see record_screen.sh for why all three are needed).
unset WAYLAND_DISPLAY XDG_SESSION_TYPE
export GDK_BACKEND=x11 MOZ_ENABLE_WAYLAND=0

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

echo "==> Launching $BROWSER_KIND (profile: $PROFILE_DIR)"
launch_login_browser

echo ""
echo "=================================================================="
echo "  Open the browser from YOUR OWN machine"
echo "=================================================================="
case "$BIND_MODE" in
  localhost)
    echo "noVNC is bound to 127.0.0.1:$NOVNC_PORT here, so nothing is exposed to"
    echo "the network. On this machine open the URL below; from another one, run:"
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
echo "Click Connect and sign in as $BOT_GOOGLE_ACCOUNT (prefilled) — only that"
echo "account. Then open zoom.us in the same window and sign in there too."
echo "Everything lands in the shared profile at:"
echo "    $PROFILE_DIR"
if [ "$SCREENSHOT_INTERVAL" -gt 0 ]; then
  echo ""
  echo "Screenshot mode is on — if noVNC won't reach, check what's on screen with:"
  echo "    scp $(id -un)@$(hostname):$SCREENSHOT_DIR/latest.png ."
fi
echo ""
echo "Close the browser window (or press Ctrl+C here) when you're done."
echo "=================================================================="
echo ""

# Block until the operator closes the browser. Ctrl+C runs the trap, which
# exits before the check; the browser closing lets it run.
wait "$BROWSER_PID" || true
cleanup
check_account
