#!/bin/bash
# Per-run virtual display + audio sink, on the host.
#
# Until the Debian 13 port these were free: every recording ran in its own
# container, so display :99 and a sink called "meeting_sink" could be
# hardcoded and still never collide. Running natively there is one X server
# namespace and one PulseAudio daemon for the whole box, so two concurrent
# meetings would grab the same display and — worse — the same sink, and each
# recording would contain both meetings' audio.
#
# So both are allocated per run here:
#   * the display number is the first free one in DISPLAY_MIN..DISPLAY_MAX,
#     claimed by creating a claim file atomically (O_EXCL), which is what
#     makes two simultaneous callers pick different numbers.
#   * the sink is named after the run, and the browser is pointed at it with
#     PULSE_SINK so its audio lands there and nowhere else.
#
# Sourced by screen/record_screen.sh and first_time_login.sh.
#
# Provides:
#   xsession_pick_display              echoes a free display NUMBER
#   xsession_start_xvfb <num> [geom]   starts Xvfb, sets XVFB_PID + DISPLAY
#   xsession_stop_xvfb                 kills it and drops the lock
#   xsession_audio_start <sink>        starts pulseaudio if needed, loads sink
#   xsession_audio_stop <sink>         unloads that sink only
#   xsession_require_tools <cmd...>    fail with install hints if any is absent
#   xsession_tailscale_ip / xsession_host_ipv4

DISPLAY_MIN="${DISPLAY_MIN:-90}"
DISPLAY_MAX="${DISPLAY_MAX:-119}"

XVFB_PID=""
XVFB_DISPLAY_NUM=""

xsession_require_tools() {
  local missing=()
  local cmd
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "ERROR: missing required program(s): ${missing[*]}" >&2
    echo "  The browser stages run natively on this Debian host now." >&2
    echo "  Install everything they need with:  sudo ./setup.sh --system" >&2
    return 1
  fi
  return 0
}

# Claim a display number by creating a claim file exclusively (noclobber is an
# O_EXCL create). Doing it before Xvfb starts means the number is ours before
# we fork, which is the whole point — two runs starting in the same second
# must not both decide ":99 looks free".
#
# The claim is OUR file (/tmp/.meeting-bot-X<n>.claim), not Xvfb's own
# /tmp/.X<n>-lock. On the VM everything ran as root and Xvfb was started with
# -nolock, so creating its lock ourselves was fine; as a normal user Xvfb
# ignores -nolock ("can only be used by root"), finds a lock naming a live pid
# — ours — and refuses to start. So Xvfb keeps its own lock, and a number is
# free only when neither lock nor socket nor a live claim exists. A claim whose
# pid is gone (a SIGKILLed run) is stale and taken over.
xsession_claim_file() { echo "/tmp/.meeting-bot-X$1.claim"; }

xsession_pick_display() {
  local n claim owner
  for (( n=DISPLAY_MIN; n<=DISPLAY_MAX; n++ )); do
    [ -e "/tmp/.X11-unix/X$n" ] && continue
    if [ -e "/tmp/.X$n-lock" ]; then
      # Xvfb takes over a lock whose pid is dead by itself; a live one is a
      # real X server (someone's desktop, another run).
      owner="$(tr -dc '0-9' < "/tmp/.X$n-lock" 2>/dev/null)"
      [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null && continue
      [ -n "$owner" ] || continue
    fi
    claim="$(xsession_claim_file "$n")"
    if [ -e "$claim" ]; then
      owner="$(tr -dc '0-9' < "$claim" 2>/dev/null)"
      if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
        continue
      fi
      rm -f "$claim" 2>/dev/null || continue
    fi
    if ( set -o noclobber; printf '%s\n' "$$" > "$claim" ) 2>/dev/null; then
      echo "$n"
      return 0
    fi
  done
  echo "ERROR: no free X display between :$DISPLAY_MIN and :$DISPLAY_MAX." >&2
  echo "  Stale claims from a crashed run look like this:  ls -l /tmp/.meeting-bot-X*.claim /tmp/.X*-lock" >&2
  return 1
}

xsession_start_xvfb() {
  local num="$1"
  local geometry="${2:-1920x1080}"
  local -a lockopt=()
  # Root may skip Xvfb's own lock; everyone else gets it whether asked or not.
  [ "$(id -u)" -eq 0 ] && lockopt=(-nolock)
  Xvfb ":$num" -screen 0 "${geometry}x24" "${lockopt[@]}" >/dev/null 2>&1 &
  XVFB_PID=$!
  XVFB_DISPLAY_NUM="$num"
  export DISPLAY=":$num"

  # Wait for the socket rather than sleeping a fixed second: the browser
  # failing with "cannot open display" is an ugly way to find out we were
  # too quick.
  local waited=0
  while [ ! -e "/tmp/.X11-unix/X$num" ]; do
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
      echo "ERROR: Xvfb exited immediately on :$num" >&2
      # Release the number we claimed, or it stays taken until a reboot.
      xsession_stop_xvfb
      return 1
    fi
    sleep 0.2
    waited=$((waited + 1))
    if [ "$waited" -gt 50 ]; then
      echo "ERROR: Xvfb did not come up on :$num within 10s" >&2
      xsession_stop_xvfb
      return 1
    fi
  done
  return 0
}

xsession_stop_xvfb() {
  # Every line ends in `|| true`: this runs from EXIT traps in `set -e`
  # scripts, where the first failing command aborts the trap AND becomes the
  # script's exit status. That bug made every successful recording exit 1.
  [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null || true
  [ -n "$XVFB_DISPLAY_NUM" ] && rm -f "$(xsession_claim_file "$XVFB_DISPLAY_NUM")" || true
  # Root ran Xvfb with -nolock, so there is no Xvfb lock of its own to clean;
  # a user's Xvfb removes its lock itself on SIGTERM.
  true
}

# --- audio -------------------------------------------------------------------

xsession_audio_daemon() {
  # PipeWire's pulse socket answers pactl just as well, so only start our own
  # daemon when nothing is listening. --exit-idle-time=-1 keeps it alive
  # between meetings; without it the daemon quits and takes every other run's
  # sink with it.
  if ! pactl info >/dev/null 2>&1; then
    echo "==> Starting PulseAudio"
    pulseaudio -D --exit-idle-time=-1 >/dev/null 2>&1 || true
    local waited=0
    while ! pactl info >/dev/null 2>&1; do
      sleep 0.3
      waited=$((waited + 1))
      if [ "$waited" -gt 30 ]; then
        echo "ERROR: PulseAudio did not start (pactl info fails)." >&2
        echo "  Running as root needs a session bus or --system; see README." >&2
        return 1
      fi
    done
  fi
  return 0
}

# xsession_audio_start <sink_name>
# Loads a null sink for THIS run and echoes nothing; the caller exports
# PULSE_SINK so the browser plays into it, and records <sink>.monitor.
xsession_audio_start() {
  local sink="$1"
  [ -n "$sink" ] || { echo "xsession_audio_start needs a sink name" >&2; return 1; }
  xsession_audio_daemon || return 1

  if pactl list short sinks 2>/dev/null | awk '{print $2}' | grep -qx "$sink"; then
    echo "==> Reusing existing sink '$sink'"
  else
    echo "==> Creating virtual sink '$sink'"
    pactl load-module module-null-sink \
      sink_name="$sink" \
      sink_properties=device.description="$sink" >/dev/null || return 1
  fi
  # NOT set-default-sink: that is global state, and flipping it would move
  # another concurrent run's browser audio into this run's recording. The
  # browser is pointed here with PULSE_SINK instead, which is per-process.
  export PULSE_SINK="$sink"
  return 0
}

xsession_audio_stop() {
  local sink="$1"
  [ -n "$sink" ] || return 0
  local idx
  # Whole-word match: record_screen.sh also loads "<sink>_mic", and a prefix
  # match would unload that one in this one's place.
  idx="$(pactl list short modules 2>/dev/null \
         | grep -E "sink_name=${sink}([[:space:]]|\$)" | awk '{print $1}' | head -n 1)" || true
  [ -n "$idx" ] && pactl unload-module "$idx" 2>/dev/null || true
  true
}

# Turn a run id into something pactl accepts as a sink name.
xsession_sink_name() {
  local raw="$1"
  local safe
  safe="$(printf '%s' "$raw" | tr -c 'A-Za-z0-9_' '_' | cut -c1-40)"
  echo "meeting_${safe}"
}

# --- addressing helpers (used when printing a URL the operator can open) -----

xsession_tailscale_ip() {
  if command -v tailscale >/dev/null 2>&1; then
    tailscale ip -4 2>/dev/null | head -n 1
  fi
}

xsession_host_ipv4() {
  # The source address the kernel would use to reach the public internet —
  # the canonical "this host's primary IPv4" on every modern Linux.
  ip -4 route get 1.1.1.1 2>/dev/null \
    | awk '/src/ {for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}'
}
