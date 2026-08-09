#!/bin/bash
# Option 1: Screen-record a meeting to MP4 (video + audio).
#
# This is the HOST-side entry point. The actual work — Xvfb, PulseAudio, real
# Google Chrome, Playwright, ffmpeg — happens inside the Debian recorder
# container, because none of it runs on Alpine's musl libc. See CLAUDE.md.
#
# By itself this does NOT transcribe or summarize; it only produces the MP4.
# pipeline.sh chains it to the later stages.
#
# Usage:
#   ./screen/record_screen.sh "<meeting_url>" "Meeting Name" [Display Name] [output.mp4]
#
# Output:
#   /opt/meeting-bot/recordings/<name>_<timestamp>.mp4   (unless output.mp4 given)
#
# Kill switch:
#   - Ctrl+\ in this terminal
#   - ./kill_meeting.sh from any other terminal
#
# Concurrency: every run gets its own container, so display :99 and the
# "meeting_sink" audio sink are namespaced per run and two meetings can record
# simultaneously without touching each other.
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
REPO_ROOT="$ROOT_DIR"
export REPO_ROOT

# shellcheck disable=SC1091
. "$ROOT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$ROOT_DIR/docker/recorder_lib.sh"

STAMP=$(date +%Y%m%d_%H%M%S)
SAFE_NAME=$(echo "$MEETING_NAME" | tr ' ' '_' | tr -cd 'A-Za-z0-9_-')
RECORDING_DIR="$MEETING_BOT_ROOT/recordings"

if [ -n "$EXPLICIT_OUTPUT" ]; then
  MP4_FILE="$EXPLICIT_OUTPUT"
else
  MP4_FILE="${RECORDING_DIR}/${SAFE_NAME}_${STAMP}.mp4"
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
rm -f "$KILL_SENTINEL"

recorder_require_docker
recorder_require_image

# Container name is derived from the run dir so kill_meeting.sh can find it and
# two concurrent recordings never fight over the same name.
CONTAINER_NAME="meeting-bot-rec-$(basename "$MEETING_BOT_RUN_DIR")"
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

KILLED=0
on_kill_signal() {
  if [ "$KILLED" -eq 0 ]; then
    KILLED=1
    echo ""
    echo "==> Kill signal received — asking the bot to leave the meeting."
    # The run dir is bind-mounted, so touching the sentinel here is visible to
    # capture.py inside the container on its next poll. That's deliberately not
    # `docker kill`: the bot clicks Leave so other participants see it go.
    touch "$KILL_SENTINEL"
  fi
}
cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap on_kill_signal INT TERM QUIT
trap cleanup EXIT

echo "==> Recording in container: $CONTAINER_NAME"
# attach mode: docker run stays in the foreground, so this script's exit code
# is the in-container script's exit code and pipeline.sh can act on it.
recorder_run "$CONTAINER_NAME" attach \
  -- bash /app/screen/record_in_container.sh \
       "$MEETING_URL" "$DISPLAY_NAME" "$MP4_FILE" "$FFMPEG_LOG"
