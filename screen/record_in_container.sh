#!/bin/bash
# Runs INSIDE the recorder container. Launched by screen/record_screen.sh on
# the Alpine host — don't run this directly there, it needs Xvfb, PulseAudio,
# ffmpeg and real Chrome, none of which exist on the host.
#
# This is the body of what used to be record_screen.sh: start the virtual
# display, set up the audio sink, join the meeting with capture.py, and record
# the display + sink audio into an MP4 once the bot is actually admitted.
#
# Usage (inside the container):
#   record_in_container.sh <meeting_url> <display_name> <output_mp4> <ffmpeg_log>
#
# Display :99 and the sink name "meeting_sink" are hardcoded and that is fine:
# each recording gets its own container, hence its own PID/IPC/network
# namespace, so two concurrent meetings can't collide on either.
set -e

MEETING_URL="$1"
DISPLAY_NAME="${2:-Meeting Bot}"
MP4_FILE="$3"
FFMPEG_LOG="$4"

if [ -z "$MEETING_URL" ] || [ -z "$MP4_FILE" ]; then
  echo "Usage: $0 <meeting_url> <display_name> <output_mp4> <ffmpeg_log>" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

export DISPLAY=:99

# Per-run sentinels, bind-mounted from the host. capture.py resolves the same
# paths from MEETING_BOT_RUN_DIR.
RUN_DIR="${MEETING_BOT_RUN_DIR:-/tmp}"
if [ "$RUN_DIR" = "/tmp" ]; then
  KILL_SENTINEL="/tmp/meeting_bot_kill"
  ADMITTED_MARKER="/tmp/meeting_bot_admitted"
else
  KILL_SENTINEL="$RUN_DIR/kill"
  ADMITTED_MARKER="$RUN_DIR/admitted"
fi
rm -f "$ADMITTED_MARKER"

mkdir -p "$(dirname "$MP4_FILE")"

KILLED=0
FFMPEG_PID=""
XVFB_PID=""
JOIN_PID=""

cleanup() {
  [ -n "$FFMPEG_PID" ] && kill -INT "$FFMPEG_PID" 2>/dev/null
  [ -n "$XVFB_PID" ] && kill "$XVFB_PID" 2>/dev/null
  true
}

on_kill_signal() {
  if [ "$KILLED" -eq 0 ]; then
    KILLED=1
    echo ""
    echo "==> Kill signal received — asking the bot to leave the meeting."
    touch "$KILL_SENTINEL"
  fi
}

trap on_kill_signal INT TERM QUIT
trap cleanup EXIT

echo "==> Starting virtual display :99 (1920x1080)"
# The Xvfb head, Chrome's --kiosk window (capture.py) and ffmpeg's -video_size
# below must all agree on 1920x1080, or the recording gets black edges.
Xvfb :99 -screen 0 1920x1080x24 &
XVFB_PID=$!
sleep 1

echo "==> Setting up virtual audio"
bash "$ROOT_DIR/audio-setup.sh"

echo "==> Joining meeting: $MEETING_URL"
python3 "$SCRIPT_DIR/capture.py" "$MEETING_URL" "$DISPLAY_NAME" &
JOIN_PID=$!

echo "==> Waiting for admission before starting the recorder..."
ADMIT_WAIT_LIMIT=620
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

echo "==> Admitted. Recording screen + audio -> $MP4_FILE"
# -preset ultrafast keeps CPU low enough not to drop frames on a 4-vCPU box;
# -crf 28 is visually fine for slides and talking heads. See CLAUDE.md before
# changing either.
ffmpeg -y \
  -f x11grab -video_size 1920x1080 -framerate 15 -i :99 \
  -f pulse -i meeting_sink.monitor \
  -c:v libx264 -preset ultrafast -crf 28 \
  -c:a aac -b:a 128k \
  -pix_fmt yuv420p \
  -shortest \
  "$MP4_FILE" \
  > "$FFMPEG_LOG" 2>&1 &
FFMPEG_PID=$!

echo "==> Recording. Waiting for the meeting to end..."
wait "$JOIN_PID" || true

echo "==> Meeting ended (or the join script exited). Stopping the recording."
# -INT lets ffmpeg finalize the MP4 cleanly; -KILL would truncate it.
kill -INT "$FFMPEG_PID" 2>/dev/null || true
wait "$FFMPEG_PID" 2>/dev/null || true

if [ ! -s "$MP4_FILE" ]; then
  echo "ERROR: MP4 is empty or missing — the recording failed." >&2
  echo "  See $FFMPEG_LOG for details." >&2
  exit 1
fi

rm -f "$KILL_SENTINEL"

echo "==> Done."
echo "Recording: $MP4_FILE"
echo "ffmpeg log: $FFMPEG_LOG"
