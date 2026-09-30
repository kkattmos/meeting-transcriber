#!/bin/bash
# Stage 1: screen-record a meeting to MP4 (video + audio), natively.
#
# Xvfb -> audio sink -> the browser (Firefox ESR or Chrome, via capture.py) -> ffmpeg x11grab.
# On the Alpine branch all of this lived in a Debian container because Chrome
# has no musl build; the host is Debian 13 now, so it runs here directly and
# there is no docker daemon, no image to build, and no bind mounts to keep in
# sync. What the container used to give away for free — a private display and
# a private audio sink per run — is allocated explicitly by lib/xsession.sh.
#
# By itself this does NOT transcribe or summarize; it only produces the MP4.
# pipeline.sh chains it to the later stages.
#
# RECORD_MEDIA=audio (pipeline.sh --record-media audio) records the meeting's
# audio alone, as AAC 128k in an .m4a — the same track the MP4 carries. The
# browser still has to run to join, admit and leave, but on a smaller
# display (RECORD_AUDIO_GEOMETRY, 960x540): nobody watches the picture, and
# painting it is the single biggest CPU cost of a meeting.
#
# Usage:
#   ./screen/record_screen.sh "<meeting_url>" "Meeting Name" [Display Name] [output.mp4]
#
# Output:
#   $RECORDINGS_DIR/<name>_<timestamp>.mp4   (unless output.mp4 is given)
#
# Kill switch:
#   - Ctrl+\ in this terminal
#   - ./kill_meeting.sh from any other terminal
set -euo pipefail

if [ -z "${1:-}" ]; then
  echo "Usage: $0 <meeting_url> [meeting_name] [display_name] [output_mp4]" >&2
  exit 1
fi

MEETING_URL="$1"
MEETING_NAME="${2:-meeting}"
DISPLAY_NAME="${3:-Meeting Bot}"
EXPLICIT_OUTPUT="${4:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# shellcheck disable=SC1091
. "$ROOT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$ROOT_DIR/lib/paths.sh"
# shellcheck disable=SC1091
. "$ROOT_DIR/lib/xsession.sh"

RECORD_MEDIA="${RECORD_MEDIA:-video}"
case "$RECORD_MEDIA" in
  video) GEOMETRY="${RECORD_GEOMETRY:-1920x1080}"; MEDIA_EXT=mp4 ;;
  audio) GEOMETRY="${RECORD_AUDIO_GEOMETRY:-960x540}"; MEDIA_EXT=m4a ;;
  *) echo "RECORD_MEDIA must be video or audio, got: $RECORD_MEDIA" >&2; exit 1 ;;
esac
# browser.py sizes the window from RECORD_GEOMETRY; it has to match the head.
export RECORD_GEOMETRY="$GEOMETRY"
FRAMERATE="${RECORD_FRAMERATE:-15}"
ADMIT_WAIT_LIMIT="${ADMIT_WAIT_SECONDS:-620}"

STAMP=$(date +%Y%m%d_%H%M%S)
SAFE_NAME=$(printf '%s' "$MEETING_NAME" | tr ' ' '_' | tr -cd 'A-Za-z0-9_-')
[ -n "$SAFE_NAME" ] || SAFE_NAME="meeting"

if [ -n "$EXPLICIT_OUTPUT" ]; then
  MP4_FILE="$EXPLICIT_OUTPUT"
else
  paths_require RECORDINGS_DIR || exit 1
  MP4_FILE="${RECORDINGS_DIR}/${SAFE_NAME}_${STAMP}.${MEDIA_EXT}"
fi
FFMPEG_LOG="${MP4_FILE%.*}_ffmpeg.log"
mkdir -p "$(dirname "$MP4_FILE")"

# Per-run sentinel directory. pipeline.sh passes one in; a standalone
# invocation gets a throwaway so it still can't collide with a parallel run.
if [ -z "${MEETING_BOT_RUN_DIR:-}" ]; then
  MEETING_BOT_RUN_DIR="$MEETING_BOT_ROOT/runs/standalone_${SAFE_NAME}_${STAMP}"
  export MEETING_BOT_RUN_DIR
fi
mkdir -p "$MEETING_BOT_RUN_DIR"
KILL_SENTINEL="$MEETING_BOT_RUN_DIR/kill"
ADMITTED_MARKER="$MEETING_BOT_RUN_DIR/admitted"
PID_FILE="$MEETING_BOT_RUN_DIR/record.pid"
rm -f "$KILL_SENTINEL" "$ADMITTED_MARKER"

PYTHON_BIN="${MEETING_BOT_VENV:-$LOADER_DIR/.venv}/bin/python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"

# MEETING_BROWSER (screen/browser.py): firefox-esr by default, chrome as the
# fallback. Each needs a different binary and a different Python driver.
BROWSER_KIND="$("$PYTHON_BIN" "$SCRIPT_DIR/browser.py" info 2>/dev/null \
                | awk '/^browser:/ {print $2}')"
if [ "$BROWSER_KIND" = "chrome" ]; then
  xsession_require_tools Xvfb ffmpeg pactl google-chrome-stable || exit 1
  DRIVER_MODULE="playwright"
else
  xsession_require_tools Xvfb ffmpeg pactl || exit 1
  if ! "$PYTHON_BIN" "$SCRIPT_DIR/browser.py" info | grep -q '^binary:  /'; then
    echo "ERROR: firefox-esr not found (sudo apt-get install firefox-esr, or set FIREFOX_BIN)." >&2
    exit 1
  fi
  DRIVER_MODULE="selenium"
fi
if ! "$PYTHON_BIN" -c "import $DRIVER_MODULE" >/dev/null 2>&1; then
  echo "ERROR: $DRIVER_MODULE is not installed in $PYTHON_BIN." >&2
  echo "  Run ./setup.sh — it builds the project .venv with uv." >&2
  exit 1
fi

RUN_ID="$(basename "$MEETING_BOT_RUN_DIR")"
SINK_NAME="$(xsession_sink_name "$RUN_ID")"

KILLED=0
FFMPEG_PID=""
JOIN_PID=""
AUDIO_WATCH_PID=""
AUDIO_LEVEL_FILE="$MEETING_BOT_RUN_DIR/audio_level"
SILENCE_WARN_SECONDS="${AUDIO_SILENCE_WARN_SECONDS:-120}"

# While recording: sample 3s of the meeting audio every 10s. A presentation
# shared without its tab audio records nine minutes of digital zero and
# nothing says so until transcription fails (2026-09-29) — this says so while
# the call is still on, in the stage log and in runs/<id>/audio_level
# ("<epoch> <peak dB> <seconds silent>"), which the web UI shows.
audio_watch() {
  local peak now silent_since="" warned=0 heard=0 silent_for
  while [ -n "$FFMPEG_PID" ] && kill -0 "$FFMPEG_PID" 2>/dev/null; do
    [ -n "$JOIN_PID" ] && "$PYTHON_BIN" "$ROOT_DIR/lib/pinaudio.py" "$JOIN_PID" "$SINK_NAME" 2>/dev/null || true
    peak="$(timeout 8 ffmpeg -hide_banner -nostats -f pulse -i "${SINK_NAME}.monitor" \
              -t 3 -af volumedetect -f null - 2>&1 \
            | sed -n 's/.*max_volume: \(-\{0,1\}[0-9.]*\) dB.*/\1/p' | tail -n 1)" || true
    now="$(date +%s)"
    if [ -n "$peak" ]; then
      # Below -60 dB is silence (Meet's own silence is -91, digital zero).
      if [ "${peak%%.*}" -lt -60 ] 2>/dev/null; then
        [ -n "$silent_since" ] || silent_since="$now"
        silent_for=$(( now - silent_since ))
        if [ "$warned" -eq 0 ] && [ "$silent_for" -ge "$SILENCE_WARN_SECONDS" ]; then
          echo "WARNING: the meeting audio has been silent for $(( silent_for / 60 )) min."
          echo "         Presenting? Share a Chrome/Edge TAB with 'Also share tab audio'."
          warned=1
        fi
      else
        silent_for=0
        if [ "$heard" -eq 0 ] || [ "$warned" -eq 1 ]; then
          echo "==> Hearing meeting audio (peak ${peak} dB)."
        fi
        heard=1; warned=0; silent_since=""
      fi
      printf '%s %s %s\n' "$now" "$peak" "$silent_for" > "$AUDIO_LEVEL_FILE.tmp" \
        && mv -f "$AUDIO_LEVEL_FILE.tmp" "$AUDIO_LEVEL_FILE"
    fi
    sleep 10
  done
}

# Stop ffmpeg with exactly ONE SIGINT and wait until it has really exited.
# A second SIGINT while ffmpeg is closing the file makes it abandon the final
# writes — "Error closing file: Immediate exit requested" in its log, a file
# with an mdat of size 0 and no moov, unplayable — while it still prints
# "Exiting normally". Two recordings were lost that way on 2026-09-29, when
# kill_meeting.sh signalled ffmpeg and this script signalled it again. The
# wait is a loop because a trapped signal (TERM from kill_meeting.sh)
# interrupts `wait` without ffmpeg having finished.
FFMPEG_SIGNALLED=0
stop_ffmpeg() {
  [ -n "$FFMPEG_PID" ] || return 0
  if [ "$FFMPEG_SIGNALLED" -eq 0 ]; then
    kill -INT "$FFMPEG_PID" 2>/dev/null || true
    FFMPEG_SIGNALLED=1
  fi
  while kill -0 "$FFMPEG_PID" 2>/dev/null; do
    wait "$FFMPEG_PID" 2>/dev/null || sleep 0.5
  done
  FFMPEG_PID=""
}

cleanup() {
  # `|| true` on every line — see lib/xsession.sh's note: a failing command in
  # an EXIT trap under errexit becomes the script's exit status, which once
  # made every successful recording look like a failed `record` stage.
  [ -n "$AUDIO_WATCH_PID" ] && kill "$AUDIO_WATCH_PID" 2>/dev/null || true
  [ -n "$JOIN_PID" ] && kill "$JOIN_PID" 2>/dev/null || true
  # Before the sink and the display go away: ffmpeg is still reading them.
  stop_ffmpeg || true
  xsession_audio_stop "$SINK_NAME" || true
  xsession_stop_xvfb || true
  rm -f "$PID_FILE" || true
  true
}

on_kill_signal() {
  if [ "$KILLED" -eq 0 ]; then
    KILLED=1
    echo ""
    echo "==> Kill signal received — asking the bot to leave the meeting."
    # Not a `kill -9` at the browser: capture.py polls for this sentinel and
    # clicks Leave, so the other participants see the bot go.
    touch "$KILL_SENTINEL"
  fi
}

trap on_kill_signal INT TERM QUIT
trap cleanup EXIT

DISPLAY_NUM="$(xsession_pick_display)" || exit 1
echo "==> Starting virtual display :$DISPLAY_NUM ($GEOMETRY)"
# The Xvfb head, Chrome's --kiosk window (capture.py) and ffmpeg's -video_size
# must all agree, or the recording gets black edges.
xsession_start_xvfb "$DISPLAY_NUM" "$GEOMETRY" || exit 1

echo "==> Setting up virtual audio (sink: $SINK_NAME)"
xsession_audio_start "$SINK_NAME" || exit 1

# Exported, so the browser — started by capture.py — renders on our display
# and plays into our sink rather than the desktop's display and speakers.
export DISPLAY=":$DISPLAY_NUM"
export PULSE_SINK="$SINK_NAME"
# The bot's audio client gets its own name. WirePlumber remembers routing
# per application name, and "Firefox" is also the operator's own browser: a
# stream the operator moved in pavucontrol was restored onto the bot's, which
# then played the meeting into the wrong device and recorded silence
# (2026-09-29). audio_watch below also pins the streams (lib/pinaudio.py).
# The microphone needs no dummy device any more: the browser blocks it.
export PULSE_PROP_OVERRIDE='application.name="Meeting Bot" application.id="meeting-bot"'
# On a Wayland desktop session the browser would otherwise pick Wayland over
# DISPLAY — putting the kiosk window on the operator's screen, or (Firefox's
# GTK, verified 2026-09-29) failing with "cannot open display" — instead of
# drawing on the Xvfb head ffmpeg records.
unset WAYLAND_DISPLAY XDG_SESSION_TYPE
export GDK_BACKEND=x11 MOZ_ENABLE_WAYLAND=0

echo "==> Joining meeting: $MEETING_URL"
# -u: unbuffered, so the join/admit/mute progress reaches the stage log as it
# happens rather than when the meeting ends.
"$PYTHON_BIN" -u "$SCRIPT_DIR/capture.py" "$MEETING_URL" "$DISPLAY_NAME" &
JOIN_PID=$!

# kill_meeting.sh reads this to escalate past the grace period without having
# to guess which pids belong to which run.
printf 'record=%s\njoin=%s\ndisplay=%s\nsink=%s\n' \
  "$$" "$JOIN_PID" "$DISPLAY_NUM" "$SINK_NAME" > "$PID_FILE"

echo "==> Waiting for admission before starting the recorder..."
waited=0
while [ ! -f "$ADMITTED_MARKER" ] && [ "$waited" -lt "$ADMIT_WAIT_LIMIT" ]; do
  if ! kill -0 "$JOIN_PID" 2>/dev/null; then
    echo "Join script exited before admission (join failed, or not admitted)." >&2
    exit 1
  fi
  if [ "$KILLED" -eq 1 ]; then
    echo "==> Kill requested while waiting for admission — aborting." >&2
    wait "$JOIN_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 2
  waited=$((waited + 2))
done

if [ ! -f "$ADMITTED_MARKER" ]; then
  echo "Timed out waiting for admission — stopping." >&2
  kill "$JOIN_PID" 2>/dev/null || true
  exit 1
fi

if [ "$RECORD_MEDIA" = "audio" ]; then
  echo "==> Admitted. Recording audio only -> $MP4_FILE"
  ffmpeg -y \
    -f pulse -i "${SINK_NAME}.monitor" \
    -c:a aac -b:a 128k \
    "$MP4_FILE" \
    > "$FFMPEG_LOG" 2>&1 &
else
  echo "==> Admitted. Recording screen + audio -> $MP4_FILE"
  # -preset ultrafast keeps CPU low enough not to drop frames on a 4-vCPU box;
  # -crf 28 is visually fine for slides and talking heads. See CLAUDE.md before
  # changing either.
  ffmpeg -y \
    -f x11grab -video_size "$GEOMETRY" -framerate "$FRAMERATE" -i ":$DISPLAY_NUM" \
    -f pulse -i "${SINK_NAME}.monitor" \
    -c:v libx264 -preset ultrafast -crf 28 \
    -c:a aac -b:a 128k \
    -pix_fmt yuv420p \
    -shortest \
    "$MP4_FILE" \
    > "$FFMPEG_LOG" 2>&1 &
fi
FFMPEG_PID=$!
printf 'record=%s\njoin=%s\nffmpeg=%s\ndisplay=%s\nsink=%s\n' \
  "$$" "$JOIN_PID" "$FFMPEG_PID" "$DISPLAY_NUM" "$SINK_NAME" > "$PID_FILE"
audio_watch &
AUDIO_WATCH_PID=$!

echo "==> Recording. Waiting for the meeting to end..."
# A loop, not one `wait`: a trapped TERM (kill_meeting.sh) interrupts `wait`
# while the bot is still leaving the call.
while kill -0 "$JOIN_PID" 2>/dev/null; do
  wait "$JOIN_PID" 2>/dev/null || true
done

echo "==> Meeting ended (or the join script exited). Stopping the recording."
# -INT lets ffmpeg finalize the MP4 cleanly; -KILL would truncate it.
kill "$AUDIO_WATCH_PID" 2>/dev/null || true
AUDIO_WATCH_PID=""
stop_ffmpeg

if [ ! -s "$MP4_FILE" ]; then
  echo "ERROR: the recording is empty or missing — it failed." >&2
  echo "  See $FFMPEG_LOG for details." >&2
  exit 1
fi
# A file with bytes in it is not necessarily a playable one (see stop_ffmpeg).
# Say so here, where the cause is known, rather than let transcription upload
# it and get "Transcoding failed" back.
if ! ffprobe -v error -show_entries format=duration -of csv=p=0 "$MP4_FILE" >/dev/null 2>&1; then
  echo "ERROR: the recording was not finalised and can't be read: $MP4_FILE" >&2
  grep -a "Error closing file" "$FFMPEG_LOG" >&2 || true
  echo "  See $FFMPEG_LOG." >&2
  exit 1
fi

rm -f "$KILL_SENTINEL"

echo "==> Done."
echo "Recording: $MP4_FILE"
echo "ffmpeg log: $FFMPEG_LOG"
