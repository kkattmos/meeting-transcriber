#!/bin/bash
# Stage 1: screen-record a meeting to MP4 (video + audio), natively.
#
# Xvfb -> audio sink -> real Google Chrome (via capture.py) -> ffmpeg x11grab.
# On the Alpine branch all of this lived in a Debian container because Chrome
# has no musl build; the host is Debian 13 now, so it runs here directly and
# there is no docker daemon, no image to build, and no bind mounts to keep in
# sync. What the container used to give away for free — a private display and
# a private audio sink per run — is allocated explicitly by lib/xsession.sh.
#
# By itself this does NOT transcribe or summarize; it only produces the MP4.
# pipeline.sh chains it to the later stages.
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

GEOMETRY="${RECORD_GEOMETRY:-1920x1080}"
FRAMERATE="${RECORD_FRAMERATE:-15}"
ADMIT_WAIT_LIMIT="${ADMIT_WAIT_SECONDS:-620}"

STAMP=$(date +%Y%m%d_%H%M%S)
SAFE_NAME=$(printf '%s' "$MEETING_NAME" | tr ' ' '_' | tr -cd 'A-Za-z0-9_-')
[ -n "$SAFE_NAME" ] || SAFE_NAME="meeting"

if [ -n "$EXPLICIT_OUTPUT" ]; then
  MP4_FILE="$EXPLICIT_OUTPUT"
else
  paths_require RECORDINGS_DIR || exit 1
  MP4_FILE="${RECORDINGS_DIR}/${SAFE_NAME}_${STAMP}.mp4"
fi
FFMPEG_LOG="${MP4_FILE%.mp4}_ffmpeg.log"
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

xsession_require_tools Xvfb ffmpeg pactl google-chrome-stable || exit 1

PYTHON_BIN="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}/bin/python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"
if ! "$PYTHON_BIN" -c "import playwright" >/dev/null 2>&1; then
  echo "ERROR: playwright is not installed in $PYTHON_BIN." >&2
  echo "  Run ./setup.sh — it installs playwright and Chrome's shared libs." >&2
  exit 1
fi

RUN_ID="$(basename "$MEETING_BOT_RUN_DIR")"
SINK_NAME="$(xsession_sink_name "$RUN_ID")"

KILLED=0
FFMPEG_PID=""
JOIN_PID=""

cleanup() {
  # `|| true` on every line — see lib/xsession.sh's note: a failing command in
  # an EXIT trap under errexit becomes the script's exit status, which once
  # made every successful recording look like a failed `record` stage.
  [ -n "$FFMPEG_PID" ] && kill -INT "$FFMPEG_PID" 2>/dev/null || true
  [ -n "$JOIN_PID" ] && kill "$JOIN_PID" 2>/dev/null || true
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

# Both are exported, so Chrome — started by capture.py — renders on our
# display and plays into our sink rather than the box's default.
export DISPLAY=":$DISPLAY_NUM"
export PULSE_SINK="$SINK_NAME"

echo "==> Joining meeting: $MEETING_URL"
"$PYTHON_BIN" "$SCRIPT_DIR/capture.py" "$MEETING_URL" "$DISPLAY_NAME" &
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
FFMPEG_PID=$!
printf 'record=%s\njoin=%s\nffmpeg=%s\ndisplay=%s\nsink=%s\n' \
  "$$" "$JOIN_PID" "$FFMPEG_PID" "$DISPLAY_NUM" "$SINK_NAME" > "$PID_FILE"

echo "==> Recording. Waiting for the meeting to end..."
wait "$JOIN_PID" || true

echo "==> Meeting ended (or the join script exited). Stopping the recording."
# -INT lets ffmpeg finalize the MP4 cleanly; -KILL would truncate it.
kill -INT "$FFMPEG_PID" 2>/dev/null || true
wait "$FFMPEG_PID" 2>/dev/null || true
FFMPEG_PID=""

if [ ! -s "$MP4_FILE" ]; then
  echo "ERROR: MP4 is empty or missing — the recording failed." >&2
  echo "  See $FFMPEG_LOG for details." >&2
  exit 1
fi

rm -f "$KILL_SENTINEL"

echo "==> Done."
echo "Recording: $MP4_FILE"
echo "ffmpeg log: $FFMPEG_LOG"
