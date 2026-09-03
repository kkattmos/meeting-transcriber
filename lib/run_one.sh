#!/bin/bash
# Execute the stage DAG for ONE input. pipeline.sh calls this once per input,
# possibly several at a time; you can also call it directly to resume a single
# run without touching the others.
#
# Usage:
#   lib/run_one.sh --run-dir DIR [--force]
#
# All the per-run configuration (input, name, language, prompt) is read back
# out of the run's state.json, which pipeline.sh writes with `runstate init`.
# That's what makes a resume a single argument: everything needed to finish the
# run is already on disk.
#
# The DAG:
#
#     record ─┐                     (meeting URLs only)
#             ├─> [ transcribe ]  ─┐
#     input ──┤                    ├─> summarize
#             └─> fetch_video ──> frames
#
# transcribe and fetch_video+frames are independent once a video exists, so
# they run concurrently. summarize joins them. Each stage records its artifacts
# in state.json, so a rerun skips whatever already finished.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# shellcheck disable=SC1091
. "$ROOT_DIR/source_env.sh"

MEETING_BOT_ROOT="${MEETING_BOT_ROOT:-/opt/meeting-bot}"
RUNSTATE="$SCRIPT_DIR/runstate.py"
SLOTQUEUE="$SCRIPT_DIR/slotqueue.py"

PYTHON_BIN="/opt/meeting-bot-venv/bin/python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"

RUN_DIR=""
FORCE=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --run-dir) RUN_DIR="${2:-}"; shift 2 ;;
    --force)   FORCE=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "run_one.sh: unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$RUN_DIR" ] || { echo "run_one.sh: --run-dir is required" >&2; exit 2; }
[ -f "$RUN_DIR/state.json" ] || {
  echo "run_one.sh: no state.json in $RUN_DIR (run 'runstate.py init' first)" >&2
  exit 2
}

RUN_ID="$(basename "$RUN_DIR")"
LOG_DIR="$RUN_DIR/logs"
mkdir -p "$LOG_DIR"

rs() { "$PYTHON_BIN" "$RUNSTATE" "$@"; }
cfg() { rs get --run-dir "$RUN_DIR" --key "$1" 2>/dev/null || true; }

INPUT="$(cfg input)"
INPUT_TYPE="$(cfg input_type)"
NAME="$(cfg name)"
SAFE_NAME="$(cfg safe_name)"
LANGUAGE="$(cfg language)"
PROMPT_NAME="$(cfg prompt)"
DISPLAY_NAME="$(cfg display_name)"
[ -n "$DISPLAY_NAME" ] || DISPLAY_NAME="Meeting Bot"
[ -n "$LANGUAGE" ] || LANGUAGE="${ASSEMBLYAI_LANGUAGE:-th}"

# --- Single-writer lock ------------------------------------------------------
# mkdir is atomic on every filesystem we care about, and unlike a lockfile it
# needs no flock binary (Alpine's base image has none). A lock whose owner is
# gone is stale and gets taken over — otherwise a SIGKILL'd run could never be
# resumed, which is exactly the situation resume exists for.
LOCK_DIR="$RUN_DIR/run.lock"
acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    echo $$ > "$LOCK_DIR/pid"
    return 0
  fi
  local owner
  owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
    echo "ERROR: run $RUN_ID is already being processed by PID $owner." >&2
    echo "  Wait for it, or stop it with: ./kill_meeting.sh --run-id $RUN_ID" >&2
    return 1
  fi
  echo "==> Taking over a stale lock from PID ${owner:-unknown}"
  echo $$ > "$LOCK_DIR/pid"
  return 0
}
acquire_lock || exit 3
trap 'rm -rf "$LOCK_DIR"' EXIT

# Clear a kill sentinel left behind by a previous run, or capture.py would
# leave the meeting the moment it joins.
rm -f "$RUN_DIR/kill"

if [ "$FORCE" -eq 1 ]; then
  echo "==> --force: discarding previous stage results for $RUN_ID"
  rs reset --run-dir "$RUN_DIR"
fi

# --- Artifact paths ----------------------------------------------------------
# Derived from the run id, never from a fresh timestamp: a resume has to land on
# the same paths the previous attempt used, or it can't tell what's already done.
MP4_FILE="$MEETING_BOT_ROOT/recordings/${RUN_ID}.mp4"
TRANSCRIPT_BASE="$MEETING_BOT_ROOT/transcripts/${RUN_ID}"
FRAMES_DIR="$MEETING_BOT_ROOT/frames/${RUN_ID}"
SUMMARY_FILE="$MEETING_BOT_ROOT/summaries/${RUN_ID}.md"
mkdir -p "$MEETING_BOT_ROOT/recordings" "$MEETING_BOT_ROOT/transcripts" \
         "$MEETING_BOT_ROOT/frames" "$MEETING_BOT_ROOT/summaries"

export MEETING_BOT_RUN_DIR="$RUN_DIR"

# --- Stage helper ------------------------------------------------------------
# Runs a stage unless it's already done, streaming its output to both the run
# log and stdout with a [stage] prefix so parallel branches stay readable.
# awk (not `sed -u`) because busybox sed has no unbuffered mode.
stage_status() { rs status --run-dir "$RUN_DIR" --stage "$1"; }

run_stage() {
  local stage="$1"; shift
  local status
  status="$(stage_status "$stage")"
  if [ "$status" = "done" ]; then
    echo "[$stage] already done — skipping (use --force to redo)"
    return 0
  fi
  if [ "$status" = "failed" ]; then
    echo "[$stage] retrying after a previous failure"
  fi

  # Machine-wide queue slot. This is what makes several concurrent
  # `./pipeline.sh` sessions take turns instead of all piling onto the same
  # CPUs and APIs: whichever asks first runs first, the rest wait here.
  #
  # No-op unless QUEUE_SLOTS_<STAGE> is set — with nothing configured this
  # returns an empty ticket immediately and touches no files.
  #
  # $BASHPID, not $$: inside the parallel branch subshells $$ is still the
  # parent's pid, and the slot must be owned by the process that actually
  # holds it so a dead branch releases it.
  #
  # It has to be read into a variable FIRST. Inside "$( ... )" bash expands
  # $BASHPID to the command substitution's own throwaway subshell, which exits
  # the instant the substitution completes — the queue would then see a dead
  # holder and immediately reclaim the slot, serializing nothing.
  local holder_pid=$BASHPID
  local ticket
  ticket="$("$PYTHON_BIN" "$SLOTQUEUE" acquire \
              --component "$stage" --pid "$holder_pid" --label "$RUN_ID")" || return 1

  # The slot is released on every exit path below, including a stage that
  # fails. (If this shell is killed outright, the queue reclaims the slot by
  # noticing the pid is gone.)
  release_slot() {
    [ -n "$ticket" ] && "$PYTHON_BIN" "$SLOTQUEUE" release \
      --component "$stage" --ticket "$ticket" 2>/dev/null || true
  }

  rs start --run-dir "$RUN_DIR" --stage "$stage"
  local log="$LOG_DIR/$stage.log"
  local rc
  "$@" 2>&1 | tee -a "$log" | awk -v s="$stage" '{print "[" s "] " $0; fflush()}'
  rc=${PIPESTATUS[0]}
  release_slot

  if [ "$rc" -ne 0 ]; then
    # Keep the tail of the log in state.json so `runstate show` explains the
    # failure without the operator having to go find the log file.
    rs fail --run-dir "$RUN_DIR" --stage "$stage" \
       --error "$(tail -n 20 "$log" 2>/dev/null)"
    echo "[$stage] FAILED (exit $rc) — see $log" >&2
    return "$rc"
  fi
  return 0
}

mark_done() {
  local stage="$1"; shift
  local args=()
  local kv path
  # A stage that exits 0 without producing its artifacts is a bug in that
  # stage, but recording `done` for a file that isn't there turns it into a
  # confusing failure two stages later (summarize opening a missing
  # transcript). Refuse at the source instead: `runstate.py status` already
  # re-checks artifacts on disk, this just moves the detection to the moment
  # the claim is made.
  for kv in "$@"; do
    args+=(--artifact "$kv")
    path="${kv#*=}"
    case "$path" in
      /*)
        if [ ! -e "$path" ]; then
          rs fail --run-dir "$RUN_DIR" --stage "$stage" \
            --error "$stage reported success but did not produce $path"
          echo "[$stage] reported success but $path does not exist" >&2
          return 1
        fi
        ;;
    esac
  done
  rs done --run-dir "$RUN_DIR" --stage "$stage" "${args[@]}"
}

# --- Stage implementations ---------------------------------------------------

do_record() {
  bash "$ROOT_DIR/screen/record_screen.sh" \
    "$INPUT" "$NAME" "$DISPLAY_NAME" "$MP4_FILE"
}

do_fetch_video() {
  # Downloads to the run dir rather than a tempdir: on a resume, frames can be
  # re-extracted without paying for the download again, and the sweep in
  # runstate.py reclaims the space later.
  if ! command -v yt-dlp >/dev/null 2>&1; then
    echo "yt-dlp is not installed. Run ./setup.sh first." >&2
    return 1
  fi
  # No merge step on purpose: bestvideo+bestaudio needs a JS runtime for
  # YouTube extraction and a postprocess merge that fails on this box. The
  # chain below tries a muxed stream first, then falls back to a *video-only*
  # stream — which is all this stage is for, since frames need no audio and
  # YouTube transcripts come from captions, never from this file. See
  # CLAUDE.md before changing the format string.
  yt-dlp --no-playlist -f "best[ext=mp4]/best/bv*[ext=mp4][vcodec^=avc1][height<=720]/bv*[ext=mp4][height<=720]/bv*[height<=720]/bv*" \
    -o "$RUN_DIR/video.%(ext)s" "$INPUT"
}

do_transcribe() {
  local src="$1"
  bash "$ROOT_DIR/transcribe/transcribe.sh" \
    "$src" "$SAFE_NAME" "$LANGUAGE" --out-base "$TRANSCRIPT_BASE"
}

do_frames() {
  local video="$1"
  "$PYTHON_BIN" "$ROOT_DIR/screen/extract_frames.py" \
    "$video" "$FRAMES_DIR" "$SAFE_NAME"
}

do_summarize() {
  local video="$1"
  local args=(
    "$ROOT_DIR/summarize/summarize.py"
    "$video"
    "${TRANSCRIPT_BASE}.txt"
    "$SUMMARY_FILE"
    --frames-manifest "$FRAMES_DIR/manifest.json"
    # On the YouTube path $video is the local download, so the original URL has
    # to be threaded through separately — it's what the document header cites
    # and what the video title is looked up from.
    --source-url "$INPUT"
    # So the document's provenance comment names the run dir the
    # artifacts actually live in, not just the meeting name.
    --run-id "$RUN_ID"
  )
  [ -n "$PROMPT_NAME" ] && args+=(--prompt "$PROMPT_NAME")
  "$PYTHON_BIN" "${args[@]}"
}

# --- Resolve the video for this input type -----------------------------------
# Sets VIDEO_FILE, or leaves it empty when the video branch has to run first.
resolve_video() {
  case "$INPUT_TYPE" in
    local_file) VIDEO_FILE="$INPUT" ;;
    meeting)    VIDEO_FILE="$MP4_FILE" ;;
    youtube)
      VIDEO_FILE="$(rs get --run-dir "$RUN_DIR" --key stages.fetch_video.artifacts.video 2>/dev/null || true)"
      if [ -z "$VIDEO_FILE" ]; then
        VIDEO_FILE="$(ls -1 "$RUN_DIR"/video.* 2>/dev/null | head -n 1)"
      fi
      ;;
  esac
}

echo "=================================================================="
echo "Run: $RUN_ID"
echo "  input:    $INPUT ($INPUT_TYPE)"
echo "  language: $LANGUAGE   prompt: ${PROMPT_NAME:-(default)}"
echo "=================================================================="

# --- Stage 1: record (meeting URLs only) -------------------------------------
if [ "$INPUT_TYPE" = "meeting" ]; then
  if ! run_stage record do_record; then
    exit 1
  fi
  mark_done record "video=$MP4_FILE" || exit 1
else
  echo "[record] skipped — $INPUT_TYPE input has no meeting to join"
fi

# --- The parallel middle: transcribe  ∥  fetch_video -> frames ---------------
# Branch A transcribes; branch B makes sure a video exists and extracts frames.
# They share nothing but the run's state file, whose writes are flock'd.

branch_transcribe() {
  # YouTube goes to youtube-transcript.io with the URL itself (captions, no
  # download); everything else sends the media file to AssemblyAI.
  local src="$INPUT"
  if [ "$INPUT_TYPE" != "youtube" ]; then
    resolve_video
    src="$VIDEO_FILE"
  fi
  run_stage transcribe do_transcribe "$src" || return 1
  mark_done transcribe "txt=${TRANSCRIPT_BASE}.txt" "srt=${TRANSCRIPT_BASE}.srt"
}

branch_frames() {
  if [ "$INPUT_TYPE" = "youtube" ]; then
    if [ "$(stage_status fetch_video)" != "done" ]; then
      run_stage fetch_video do_fetch_video || return 1
      local got
      got="$(ls -1 "$RUN_DIR"/video.* 2>/dev/null | head -n 1)"
      if [ -z "$got" ]; then
        rs fail --run-dir "$RUN_DIR" --stage fetch_video \
          --error "yt-dlp reported success but produced no file in $RUN_DIR"
        echo "[fetch_video] yt-dlp produced no file" >&2
        return 1
      fi
      mark_done fetch_video "video=$got" || return 1
    else
      echo "[fetch_video] already done — skipping"
    fi
  fi

  resolve_video
  if [ -z "$VIDEO_FILE" ] || [ ! -f "$VIDEO_FILE" ]; then
    echo "[frames] no video available at '${VIDEO_FILE:-}' — cannot extract frames" >&2
    return 1
  fi
  run_stage frames do_frames "$VIDEO_FILE" || return 1
  mark_done frames "manifest=$FRAMES_DIR/manifest.json"
}

echo ""
echo "==> Running transcribe and frame-extraction in parallel"
branch_transcribe & PID_T=$!
branch_frames &     PID_F=$!

wait "$PID_T"; RC_T=$?
wait "$PID_F"; RC_F=$?

# Report both outcomes before bailing. Failing one branch shouldn't hide
# whether the other one also needs attention on the next resume.
if [ "$RC_T" -ne 0 ] || [ "$RC_F" -ne 0 ]; then
  echo "" >&2
  echo "==> Stage failure in run $RC_T/$RC_F:" >&2
  [ "$RC_T" -ne 0 ] && echo "    transcribe branch failed" >&2
  [ "$RC_F" -ne 0 ] && echo "    frames branch failed" >&2
  echo "    Nothing is lost — whatever succeeded is recorded. Resume with:" >&2
  echo "      ./pipeline.sh --run-id $RUN_ID" >&2
  exit 1
fi

# --- Stage 3: summarize ------------------------------------------------------
resolve_video
echo ""
if ! run_stage summarize do_summarize "$VIDEO_FILE"; then
  echo "    Resume with:  ./pipeline.sh --run-id $RUN_ID" >&2
  exit 1
fi
mark_done summarize "md=$SUMMARY_FILE" || exit 1

echo ""
echo "=================================================================="
echo "Run complete: $RUN_ID"
echo "=================================================================="
[ "$INPUT_TYPE" = "meeting" ] && echo "Recording:  $MP4_FILE"
echo "Transcript: ${TRANSCRIPT_BASE}.txt"
echo "Summary:    $SUMMARY_FILE"
