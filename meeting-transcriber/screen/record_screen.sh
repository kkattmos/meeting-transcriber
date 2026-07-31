#!/bin/bash
# Option 1: Screen-record a meeting to MP4 (video + audio).
#
# Starts Xvfb, opens the meeting in real Chrome via capture.py, and uses ffmpeg
# to capture both the virtual display (video) and meeting_sink.monitor (audio)
# into a single H.264+AAC MP4.
#
# This is the building block that pipeline.sh chains to. By itself it does NOT
# transcribe or summarize - it only produces the video file.
#
# Usage:
#   sudo -H ./screen/record_screen.sh "<meeting_url>" "Meeting Name" [Display Name]
#
# Output:
#   /opt/meeting-bot/recordings/<name>_<timestamp>.mp4
#
# Kill switch (same as the old audio-only pipeline, unchanged):
#   - Ctrl+\ in this terminal
#   - sudo ./kill_meeting.sh from any other terminal
set -e

if [ -z "$1" ]; then
  echo "Usage: $0 <meeting_url> [meeting_name] [display_name]"
  exit 1
fi

MEETING_URL="$1"
MEETING_NAME="${2:-meeting}"
DISPLAY_NAME="${3:-Meeting Bot}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
STAMP=$(date +%Y%m%d_%H%M%S)
SAFE_NAME=$(echo "$MEETING_NAME" | tr ' ' '_' | tr -cd 'A-Za-z0-9_-')
RECORDING_DIR="/opt/meeting-bot/recordings"
MP4_FILE="${RECORDING_DIR}/${SAFE_NAME}_${STAMP}.mp4"
FFMPEG_LOG="${RECORDING_DIR}/${SAFE_NAME}_${STAMP}_ffmpeg.log"

mkdir -p "$RECORDING_DIR"

export DISPLAY=:99

# --- Pre-cleanup: a previous first_time_login.sh (or a crashed earlier run)
# may have left Xvfb on :99. Without this, the next start fails with
# "Server is already active for display 99" and we silently inherit a
# stale display server.
pkill -f "Xvfb :99" 2>/dev/null || true
rm -f /tmp/.X99-lock
sleep 1

# Sentinel file consumed by capture.py to do a clean leave (it clicks
# the in-Meet "Leave" button before exiting, so participants see the bot
# go). Touched either by the signal trap below (Ctrl+\) or by kill_meeting.sh.
KILL_SENTINEL="/tmp/meeting_bot_kill"
rm -f "$KILL_SENTINEL"

KILLED=0

cleanup() {
  # Called both on normal exit and on signal. Stops ffmpeg and Xvfb if alive.
  [ -n "${FFMPEG_PID:-}" ] && kill -INT "$FFMPEG_PID" 2>/dev/null || true
  [ -n "${XVFB_PID:-}" ]   && kill "$XVFB_PID" 2>/dev/null || true
}

on_kill_signal() {
  if [ "$KILLED" -eq 0 ]; then
    KILLED=1
    echo ""
    echo "==> Kill signal received - asking the bot to leave the meeting."
    touch "$KILL_SENTINEL"
  fi
}

trap on_kill_signal INT TERM QUIT
trap cleanup EXIT

# --- Start virtual display ---
echo "==> Starting virtual display"
# 1920x1080 matches the default Proxmox console; Xvfb head, Chrome's --kiosk
# window, and ffmpeg's -video_size below all use this geometry so there are
# no black edges in the recording.
Xvfb :99 -screen 0 1920x1080x24 &
XVFB_PID=$!
sleep 1

# --- Set up audio sink ---
echo "==> Setting up virtual audio"
bash "$ROOT_DIR/audio-setup.sh"

# --- Launch the join script in the background ---
ADMITTED_MARKER="/tmp/meeting_bot_admitted"
rm -f "$ADMITTED_MARKER"

echo "==> Joining meeting: $MEETING_URL"
source /opt/meeting-bot-venv/bin/activate
python3 "$SCRIPT_DIR/capture.py" "$MEETING_URL" "$DISPLAY_NAME" &
JOIN_PID=$!
deactivate

# --- Wait for admission before starting the recorder ---
echo "==> Waiting for admission before starting recording..."
ADMIT_WAIT_LIMIT=620
waited=0
while [ ! -f "$ADMITTED_MARKER" ] && [ "$waited" -lt "$ADMIT_WAIT_LIMIT" ]; do
  if ! kill -0 "$JOIN_PID" 2>/dev/null; then
    echo "Join script exited before admission (join failed, or not admitted)."
    exit 1
  fi
  if [ "$KILLED" -eq 1 ]; then
    echo "==> Kill requested while waiting for admission - aborting."
    wait "$JOIN_PID" 2>/dev/null || true
    exit 1
  fi
  sleep 2
  waited=$((waited + 2))
done

if [ ! -f "$ADMITTED_MARKER" ]; then
  echo "Timed out waiting for admission - stopping."
  kill "$JOIN_PID" 2>/dev/null || true
  exit 1
fi

# --- Start ffmpeg: x11grab video + pulseaudio monitor audio, muxed to MP4 ---
echo "==> Admitted. Recording screen + audio -> $MP4_FILE"
# -f x11grab captures the virtual display (where the meeting browser lives).
# -f pulse records from the same meeting_sink that the existing audio pipeline
# used, so existing audio quality is preserved.
# -preset ultrafast keeps CPU low so we don't drop frames on the 4 vCPU VM.
# -crf 28 is visually fine for meeting content (slides, talking heads); saves
# disk vs the default 23.
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

echo "==> Recording. Waiting for meeting to end..."
wait "$JOIN_PID"
JOIN_EXIT=$?

echo "==> Meeting ended (or join script exited). Stopping recording."
# -INT lets ffmpeg finalize the MP4 cleanly (vs -KILL which truncates it).
kill -INT "$FFMPEG_PID" 2>/dev/null || true
wait "$FFMPEG_PID" 2>/dev/null || true

# Verify the MP4 has actual content (size > 0 and ffprobe-able).
if [ ! -s "$MP4_FILE" ]; then
  echo "WARNING: MP4 is empty or missing - recording failed."
  echo "  See $FFMPEG_LOG for details."
  exit 1
fi

# Clean up the kill sentinel so a future run starts clean.
rm -f "$KILL_SENTINEL"

echo "==> Done."
echo "Recording: $MP4_FILE"
echo "ffmpeg log: $FFMPEG_LOG"