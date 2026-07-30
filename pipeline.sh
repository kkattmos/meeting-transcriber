#!/bin/bash
# Run all three options in sequence: record -> transcribe -> summarize.
#
# Accepts a meeting URL (Google Meet / Zoom), a YouTube URL, or a local file
# already on disk (.mp4/.mkv/.m4a/.wav/...). For meeting URLs, all three
# stages run. For YouTube URLs and local files, Stage 1 (recording) is
# skipped — there's nothing to join/capture — and Stages 2 & 3 run directly
# against the YouTube URL (via yt-dlp) or the local file, respectively.
#
# Usage:
#   sudo -H ./pipeline.sh "<meeting_or_youtube_url_or_local_file>" "Meeting Name" [Display Name] [Language]
#
# This is the entry point for the phone/Tailscale trigger (trigger_server.py
# defaults to this script). It's also what most users will run from the
# terminal when they want "the whole thing."
#
# The three stages are independent and you can also run them by hand:
#   sudo -H ./screen/record_screen.sh "<url>" "<name>"           # option 1
#   sudo -H ./transcribe/transcribe.sh <video_or_audio_or_url> "<n>"  # option 2
#   python3 ./summarize/summarize.py <video_or_url> <transcript>       # option 3
set -e

# Load `.env` from the repo root (one file for every stage). No-op if no
# `.env` exists. Already-set env vars take precedence so one-off overrides
# via `KEY=val ./pipeline.sh ...` continue to work.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"

if [ -z "$1" ]; then
  echo "Usage: $0 <meeting_or_youtube_url_or_local_file> [meeting_name] [display_name] [language]"
  echo "  Input forms: https://meet.google.com/<id>, https://zoom.us/j/<id>,"
  echo "               https://www.youtube.com/watch?v=<id>, https://youtu.be/<id>,"
  echo "               or a path to a local file already on disk"
  echo "  language: th (default), en, auto, or any AssemblyAI language code"
  exit 1
fi

INPUT_URL="$1"
MEETING_NAME="${2:-meeting}"
DISPLAY_NAME="${3:-Meeting Bot}"
LANGUAGE="${4:-${ASSEMBLYAI_LANGUAGE:-th}}"

# SCRIPT_DIR is set above (before source_env.sh).

# --- Input-type routing ------------------------------------------------------
# Meeting URLs (Meet/Zoom) run all three stages: record -> transcribe -> summarize.
# YouTube URLs have no meeting to join, so Stage 1 is skipped and the URL is
# passed straight to Stages 2 & 3 — yt-dlp handles the download there.
# A local file (.mp4/.mkv/.m4a/.wav/etc, already on disk) also skips Stage 1 —
# there's nothing to record, it's fed straight to Stages 2 & 3 as-is.
IS_MEETING_URL=0
IS_YOUTUBE_URL=0
IS_LOCAL_FILE=0

if [ -f "$INPUT_URL" ]; then
  IS_LOCAL_FILE=1
elif echo "$INPUT_URL" | grep -qE '(meet\.google\.com/|^https?://[^/]*zoom\.us/)'; then
  IS_MEETING_URL=1
elif echo "$INPUT_URL" | grep -qE '(youtube\.com/watch\?v=|youtu\.be/)'; then
  IS_YOUTUBE_URL=1
fi

if [ "$IS_MEETING_URL" -eq 0 ] && [ "$IS_YOUTUBE_URL" -eq 0 ] && [ "$IS_LOCAL_FILE" -eq 0 ]; then
  echo "ERROR: Unrecognized input: $INPUT_URL"
  echo "  Expected: https://meet.google.com/<id>, https://zoom.us/<id>, a YouTube URL,"
  echo "            or a path to a local file that exists on disk (.mp4/.mkv/.m4a/.wav/...)."
  exit 1
fi

# For a local file with no explicit meeting name ($2 unset), derive one from
# the filename instead of falling back to the generic "meeting" default, so
# repeated runs on different files don't collide on the same basename.
if [ "$IS_LOCAL_FILE" -eq 1 ] && [ -z "${2:-}" ]; then
  MEETING_NAME="$(basename "$INPUT_URL")"
  MEETING_NAME="${MEETING_NAME%.*}"
fi

# SAFE_NAME sanitization: matches the rule used by transcribe.sh and capture.py
# so the same meeting name produces the same basename across all stages.
SAFE_NAME=$(echo "$MEETING_NAME" | tr ' ' '_' | tr -cd 'A-Za-z0-9_-')

# For YouTube URLs (no user-supplied meeting name in the typical workflow),
# derive a name from the video ID so transcripts and summaries share a
# consistent basename. Format: yt_<video_id>. The video ID is the 11-char
# token after watch?v= for youtube.com, or the path segment for youtu.be.
if [ "$IS_YOUTUBE_URL" -eq 1 ]; then
  # Extract the video ID. youtube.com: token after watch?v=; youtu.be: path
  # segment. Use '#' as the sed delimiter so the literal '|' in the URL
  # pattern doesn't get parsed as the s-command separator.
  YT_ID=$(echo "$INPUT_URL" | sed -nE 's#.*(youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{6,}).*#\2#p')
  if [ -z "$YT_ID" ]; then
    YT_ID="video"
  fi
  SAFE_NAME="yt_${YT_ID}"
fi

if [ "$IS_MEETING_URL" -eq 1 ]; then
  # Stage 1: record the meeting (audio + screen video -> MP4)
  echo ""
  echo "==================================================================="
  echo "Stage 1/3: Screen recording"
  echo "==================================================================="
  bash "$SCRIPT_DIR/screen/record_screen.sh" "$INPUT_URL" "$MEETING_NAME" "$DISPLAY_NAME"

  # Find the most recent MP4 in the recordings directory that matches the
  # meeting name. (record_screen.sh names files with a timestamp suffix.)
  RECORDING_DIR="/opt/meeting-bot/recordings"
  MP4_FILE=$(ls -1t "$RECORDING_DIR/${SAFE_NAME}"_*.mp4 2>/dev/null | head -n 1)

  if [ -z "$MP4_FILE" ] || [ ! -f "$MP4_FILE" ]; then
    echo "ERROR: Could not find the MP4 produced by stage 1."
    echo "  Looked in: $RECORDING_DIR for ${SAFE_NAME}_*.mp4"
    exit 1
  fi
elif [ "$IS_LOCAL_FILE" -eq 1 ]; then
  echo ""
  echo "==================================================================="
  echo "Stage 1/3: SKIPPED (local file provided directly)"
  echo "==================================================================="
  MP4_FILE="$INPUT_URL"  # already on disk — Stage 2/3 use it as-is
else
  echo ""
  echo "==================================================================="
  echo "Stage 1/3: SKIPPED (YouTube URL has no meeting to record)"
  echo "==================================================================="
  MP4_FILE=""  # not used on the YouTube path; Stage 2/3 get the URL directly
fi

# Stage 2: transcribe (MP4's audio for meetings; YouTube audio via yt-dlp)
echo ""
echo "==================================================================="
echo "Stage 2/3: Transcription (AssemblyAI)"
echo "==================================================================="
if [ "$IS_YOUTUBE_URL" -eq 1 ]; then
  bash "$SCRIPT_DIR/transcribe/transcribe.sh" "$INPUT_URL" "$SAFE_NAME" "$LANGUAGE"
else
  bash "$SCRIPT_DIR/transcribe/transcribe.sh" "$MP4_FILE" "$MEETING_NAME" "$LANGUAGE"
fi

TRANSCRIPT_DIR="/opt/meeting-bot/transcripts"
TXT_FILE=$(ls -1t "$TRANSCRIPT_DIR/${SAFE_NAME}"_*.txt 2>/dev/null | head -n 1)
if [ -z "$TXT_FILE" ] || [ ! -f "$TXT_FILE" ]; then
  echo "ERROR: Could not find the transcript produced by stage 2."
  echo "  Looked in: $TRANSCRIPT_DIR for ${SAFE_NAME}_*.txt"
  exit 1
fi

# Stage 3: summarize with the AI agent (uses MP4 frames for meetings;
# downloads the YouTube MP4 for frame extraction when applicable)
echo ""
echo "==================================================================="
echo "Stage 3/3: AI summary"
echo "==================================================================="
# Stage 3 doesn't need sudo - it's a pure CPU/network task. But if the user
# ran this script with sudo -H (matching the README's instructions), the
# venv's python3 should still resolve via the venv-activated scripts that
# ran already. We use the system python3 + the meeting-bot-venv site-packages.
SUMMARY_DIR="/opt/meeting-bot/summaries"
mkdir -p "$SUMMARY_DIR"
SUMMARY_FILE="${SUMMARY_DIR}/${SAFE_NAME}_$(date +%Y%m%d_%H%M%S).md"

# The summarize.py script can be invoked as a normal user; no sudo needed.
# We invoke it through the venv python explicitly so the anthropic SDK
# (installed in /opt/meeting-bot-venv) is available.
PYTHON_BIN="/opt/meeting-bot-venv/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi
if [ "$IS_YOUTUBE_URL" -eq 1 ]; then
  # YouTube path: pass the URL directly; summarize.py downloads the MP4
  # via yt-dlp and cleans up the temp file when done.
  "$PYTHON_BIN" "$SCRIPT_DIR/summarize/summarize.py" "$INPUT_URL" "$TXT_FILE" "$SUMMARY_FILE"
else
  "$PYTHON_BIN" "$SCRIPT_DIR/summarize/summarize.py" "$MP4_FILE" "$TXT_FILE" "$SUMMARY_FILE"
fi

echo ""
echo "==================================================================="
echo "Pipeline complete"
echo "==================================================================="
if [ "$IS_MEETING_URL" -eq 1 ]; then
  echo "Recording:  $MP4_FILE"
elif [ "$IS_LOCAL_FILE" -eq 1 ]; then
  echo "Input file: $MP4_FILE"
fi
echo "Transcript: $TXT_FILE"
echo "Summary:    $SUMMARY_FILE"
