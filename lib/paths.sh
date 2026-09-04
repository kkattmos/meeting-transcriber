#!/bin/bash
# Bash counterpart of lib/paths.py: resolve and validate the five independent
# output directories. Source it after source_env.sh.
#
#   . "$ROOT_DIR/lib/paths.sh"
#   paths_require            # all five, or exit with a message naming them
#   paths_require RECORDINGS_DIR TRANSCRIPTS_DIR    # only what this script uses
#
# Every path here may contain spaces. Nothing in this file (or in its callers)
# may expand one of these unquoted.

# Legacy alias so an .env written before the rename keeps working.
if [ -z "${FRAMES_DIR:-}" ] && [ -n "${FRAME_OUTPUT_DIR:-}" ]; then
  FRAMES_DIR="$FRAME_OUTPUT_DIR"
  export FRAMES_DIR
fi

# MEETING_BOT_ROOT is no longer the parent of the media dirs — it holds only
# the pipeline's own bookkeeping (runs/, tmp/, state/, chrome-profile/).
MEETING_BOT_ROOT="${MEETING_BOT_ROOT:-/opt/meeting-bot}"
export MEETING_BOT_ROOT

_paths_describe() {
  case "$1" in
    RECORDINGS_DIR)  echo "meeting recordings (.mp4)" ;;
    TRANSCRIPTS_DIR) echo "transcripts (.txt/.srt)" ;;
    FRAMES_DIR)      echo "extracted video frames" ;;
    SUMMARIES_DIR)   echo "summary documents (.md)" ;;
    PDF_DIR)         echo "rendered summary PDFs" ;;
    *)               echo "output files" ;;
  esac
}

# paths_require [VAR...]  — default: all five.
paths_require() {
  local -a wanted=("$@")
  if [ "${#wanted[@]}" -eq 0 ]; then
    wanted=(RECORDINGS_DIR TRANSCRIPTS_DIR FRAMES_DIR SUMMARIES_DIR PDF_DIR)
  fi
  local -a missing=()
  local var
  for var in "${wanted[@]}"; do
    if [ -z "${!var:-}" ]; then
      missing+=("$var")
    fi
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "ERROR: required output directory setting(s) not configured:" >&2
    for var in "${missing[@]}"; do
      echo "  $var   — where $(_paths_describe "$var") are written" >&2
    done
    echo "" >&2
    echo "  The five output directories are independent of each other and of" >&2
    echo "  MEETING_BOT_ROOT. Set them in the .env file at the repo root;" >&2
    echo "  .env.example ships a working set of defaults:" >&2
    echo "      cp .env.example .env && chmod 600 .env" >&2
    return 1
  fi
  return 0
}

# paths_mkdir VAR...  — create the directories those vars name.
paths_mkdir() {
  local var
  for var in "$@"; do
    [ -n "${!var:-}" ] || continue
    mkdir -p "${!var}"
  done
}
