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
#
# Kaltura is the one input type where they are NOT independent. A YouTube
# transcript comes from captions, so transcribe never needs the download; a
# Kaltura entry usually has no captions at all, and then AssemblyAI needs the
# media file. So for kaltura, fetch_video runs first, on its own, and the two
# branches start after it:
#
#     input ──> fetch_video ──┬─> transcribe ─┐
#                             └─> frames ─────┴─> summarize
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# shellcheck disable=SC1091
. "$ROOT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$ROOT_DIR/lib/paths.sh"

RUNSTATE="$SCRIPT_DIR/runstate.py"
SLOTQUEUE="$SCRIPT_DIR/slotqueue.py"

PYTHON_BIN="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}/bin/python3"
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
# Reference material for this run (slides repo / folder), one spec per line.
# Replayed on every attempt so a resumed run summarizes against the same
# material the first attempt used.
declare -a RESOURCE_SPECS=()
while IFS= read -r _spec; do
  [ -n "$_spec" ] && RESOURCE_SPECS+=("$_spec")
done < <(rs get --run-dir "$RUN_DIR" --key resources 2>/dev/null || true)
[ -n "$DISPLAY_NAME" ] || DISPLAY_NAME="Meeting Bot"

# What the summary document cites as its source. For every other input type
# that is the input itself; a Kaltura input may be a 900-character <iframe>
# tag, which would land verbatim in the provenance comment and the link line,
# so it is normalised to a plain embed URL for the same entry.
SOURCE_URL="$INPUT"
if [ "$INPUT_TYPE" = "kaltura" ]; then
  _canon="$("$PYTHON_BIN" "$SCRIPT_DIR/kaltura.py" parse "$INPUT" 2>/dev/null \
             | sed -nE 's/.*"url": "([^"]*)".*/\1/p')"
  [ -n "$_canon" ] && SOURCE_URL="$_canon"
fi
[ -n "$LANGUAGE" ] || LANGUAGE="${ASSEMBLYAI_LANGUAGE:-th}"

# --- Single-writer lock ------------------------------------------------------
# mkdir is atomic on every filesystem we care about, and unlike a lockfile it
# needs no flock binary and no cleanup path. A lock whose owner is
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
# The five directories are configured independently in .env — none of them is
# assumed to be a subdirectory of another.
paths_require || exit 2
MP4_FILE="${RECORDINGS_DIR}/${RUN_ID}.mp4"
TRANSCRIPT_BASE="${TRANSCRIPTS_DIR}/${RUN_ID}"
RUN_FRAMES_DIR="${FRAMES_DIR}/${RUN_ID}"
SUMMARY_FILE="${SUMMARIES_DIR}/${RUN_ID}.md"
SUMMARY_PDF="${PDF_DIR}/${RUN_ID}.pdf"
paths_mkdir RECORDINGS_DIR TRANSCRIPTS_DIR FRAMES_DIR SUMMARIES_DIR PDF_DIR

export MEETING_BOT_RUN_DIR="$RUN_DIR"

# --- Stage helper ------------------------------------------------------------
# Runs a stage unless it's already done, streaming its output to both the run
# log and stdout with a [stage] prefix so parallel branches stay readable.
# awk with an explicit fflush(), not `sed -u`: it flushes per line on every
# sed/awk implementation rather than relying on a GNU extension.
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
  if [ "$INPUT_TYPE" = "kaltura" ]; then
    do_fetch_kaltura
    return $?
  fi
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

# Kaltura: not yt-dlp. Its Kaltura extractor sends no Referer, which a
# university tenant's access-control answers with a bare 404 — see lib/kaltura.py.
# Unlike the YouTube path this download is on the critical path for transcribe
# too, because Kaltura entries rarely carry captions.
do_fetch_kaltura() {
  # Entry facts (title, duration, caption tracks) alongside the media, so
  # summarize can name the lecture without a second round trip and a resume
  # doesn't need one at all.
  if ! "$PYTHON_BIN" "$SCRIPT_DIR/kaltura.py" info "$INPUT" > "$RUN_DIR/kaltura.json"; then
    rm -f "$RUN_DIR/kaltura.json"
    echo "[fetch_video] could not read the Kaltura entry" >&2
    return 1
  fi
  "$PYTHON_BIN" "$SCRIPT_DIR/kaltura.py" download "$INPUT" "$RUN_DIR/video.mp4"
}

do_transcribe() {
  local src="$1"
  local args=("$src" "$SAFE_NAME" "$LANGUAGE" --out-base "$TRANSCRIPT_BASE")
  # On the Kaltura path the *input* is passed so free captions can be tried
  # first, with the downloaded MP4 behind --media as the AssemblyAI fallback.
  [ -n "${2:-}" ] && args+=(--media "$2")
  bash "$ROOT_DIR/transcribe/transcribe.sh" "${args[@]}"
}

do_frames() {
  local video="$1"
  "$PYTHON_BIN" "$ROOT_DIR/screen/extract_frames.py" \
    "$video" "$RUN_FRAMES_DIR" "$SAFE_NAME"
}

do_summarize() {
  local video="$1"
  local args=(
    "$ROOT_DIR/summarize/summarize.py"
    "$video"
    "${TRANSCRIPT_BASE}.txt"
    "$SUMMARY_FILE"
    --frames-manifest "$RUN_FRAMES_DIR/manifest.json"
    # PDF_DIR is independent of SUMMARIES_DIR, so the PDF path is passed
    # explicitly rather than derived from the .md path.
    --pdf-out "$SUMMARY_PDF"
    # On the YouTube path $video is the local download, so the original URL has
    # to be threaded through separately — it's what the document header cites
    # and what the video title is looked up from.
    --source-url "$SOURCE_URL"
    # So the document's provenance comment names the run dir the
    # artifacts actually live in, not just the meeting name.
    --run-id "$RUN_ID"
  )
  [ -n "$PROMPT_NAME" ] && args+=(--prompt "$PROMPT_NAME")
  # Kaltura has no yt-dlp to ask for a title, so the entry's own name (read at
  # fetch time into kaltura.json) is passed explicitly. Best-effort: a run
  # whose fetch predates this file just falls back to the meeting name.
  if [ "$INPUT_TYPE" = "kaltura" ] && [ -f "$RUN_DIR/kaltura.json" ]; then
    local kal_title
    kal_title="$("$PYTHON_BIN" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("title") or "")' \
                  "$RUN_DIR/kaltura.json" 2>/dev/null || true)"
    [ -n "$kal_title" ] && args+=(--title "$kal_title")
  fi
  local spec
  for spec in "${RESOURCE_SPECS[@]:-}"; do
    [ -n "$spec" ] && args+=(--resources "$spec")
  done
  "$PYTHON_BIN" "${args[@]}"
}

# --- Resolve the video for this input type -----------------------------------
# Sets VIDEO_FILE, or leaves it empty when the video branch has to run first.
resolve_video() {
  case "$INPUT_TYPE" in
    local_file) VIDEO_FILE="$INPUT" ;;
    meeting)    VIDEO_FILE="$MP4_FILE" ;;
    youtube|kaltura)
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

# The download, shared by both callers below. Idempotent: a fetch that is
# already `done` is skipped, so running it ahead of the branches (kaltura) and
# inside the frames branch (youtube) can't download twice.
ensure_video_fetched() {
  if [ "$(stage_status fetch_video)" = "done" ]; then
    echo "[fetch_video] already done — skipping"
    return 0
  fi
  run_stage fetch_video do_fetch_video || return 1
  local got
  got="$(ls -1 "$RUN_DIR"/video.* 2>/dev/null | grep -v '\.part$' | head -n 1)"
  if [ -z "$got" ]; then
    rs fail --run-dir "$RUN_DIR" --stage fetch_video \
      --error "the download reported success but produced no file in $RUN_DIR"
    echo "[fetch_video] produced no file" >&2
    return 1
  fi
  mark_done fetch_video "video=$got" || return 1
}

branch_transcribe() {
  # YouTube goes to youtube-transcript.io with the URL itself (captions, no
  # download); Kaltura passes the input too — free captions when the entry has
  # them — but also hands over the downloaded MP4 for the usual AssemblyAI
  # fallback. Everything else just sends the media file.
  local src="$INPUT"
  local media=""
  if [ "$INPUT_TYPE" = "kaltura" ]; then
    resolve_video
    media="$VIDEO_FILE"
  elif [ "$INPUT_TYPE" != "youtube" ]; then
    resolve_video
    src="$VIDEO_FILE"
  fi
  run_stage transcribe do_transcribe "$src" "$media" || return 1
  mark_done transcribe "txt=${TRANSCRIPT_BASE}.txt" "srt=${TRANSCRIPT_BASE}.srt"
}

branch_frames() {
  if [ "$INPUT_TYPE" = "youtube" ]; then
    ensure_video_fetched || return 1
  fi

  resolve_video
  if [ -z "$VIDEO_FILE" ] || [ ! -f "$VIDEO_FILE" ]; then
    echo "[frames] no video available at '${VIDEO_FILE:-}' — cannot extract frames" >&2
    return 1
  fi
  run_stage frames do_frames "$VIDEO_FILE" || return 1
  mark_done frames "manifest=$RUN_FRAMES_DIR/manifest.json"
}

# Kaltura's transcribe branch needs the media file (the entry usually has no
# captions), so the download can't sit inside the frames branch the way
# YouTube's does — it runs here, ahead of both.
if [ "$INPUT_TYPE" = "kaltura" ]; then
  echo ""
  echo "==> Fetching the Kaltura entry before transcribe and frames"
  if ! ensure_video_fetched; then
    echo "    Resume with:  ./pipeline.sh --run-id $RUN_ID" >&2
    exit 1
  fi
fi

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

# Frames are the one artifact set that is free to regenerate: the source video
# always outlives them (a recording in RECORDINGS_DIR, a YouTube download in
# runs/<id>/video.mp4), and nothing downstream reads them once the PDF exists —
# WeasyPrint embeds the image bytes into the file itself. They are also the
# bulkiest thing a run leaves behind, so they are swept by default.
#
# Timing is the whole point: this runs *after* the PDF has rendered, never
# before. If a PDF was asked for and isn't there, the render failed, and the
# cheap fix is `summarize/pdf.py <md> <pdf> --frames-manifest ...` — which needs
# exactly these frames. So that case keeps them.
#
# KEEP_FRAMES=1 opts out entirely.
cleanup_frames() {
  case "$(printf '%s' "${KEEP_FRAMES:-0}" | tr 'A-Z' 'a-z')" in
    1|true|yes|on) return 0 ;;
  esac
  # Never let an unset variable turn this into `rm -rf /` or wipe the whole
  # FRAMES_DIR: only ever the one subdirectory this run created.
  if [ -z "${FRAMES_DIR:-}" ] || [ -z "${RUN_ID:-}" ] \
     || [ "$RUN_FRAMES_DIR" = "$FRAMES_DIR" ]; then
    return 0
  fi
  [ -d "$RUN_FRAMES_DIR" ] || return 0

  local want_pdf=1
  case "$(printf '%s' "${SUMMARY_WRITE_PDF:-1}" | tr 'A-Z' 'a-z')" in
    0|false|no) want_pdf=0 ;;
  esac
  if [ "$want_pdf" -eq 1 ] && [ ! -f "$SUMMARY_PDF" ]; then
    echo "[frames] kept — the PDF did not render, and re-rendering it needs them"
    return 0
  fi

  rm -rf "$RUN_FRAMES_DIR"
  # Tell the state file the paths went on purpose. Otherwise runstate's
  # artifact check downgrades a finished `frames` stage to `pending` and every
  # later --status makes a completed run look half-broken.
  rs cleaned --run-dir "$RUN_DIR" --stage frames || true
  echo "[frames] removed $RUN_FRAMES_DIR (KEEP_FRAMES=1 to keep them)"
}

# --- Stage 3: summarize ------------------------------------------------------
resolve_video
echo ""
if ! run_stage summarize do_summarize "$VIDEO_FILE"; then
  echo "    Resume with:  ./pipeline.sh --run-id $RUN_ID" >&2
  exit 1
fi
# Either output can be switched off (SUMMARY_WRITE_MARKDOWN / SUMMARY_WRITE_PDF,
# or --no-markdown / --no-pdf), and a PDF that fails to render is a warning
# rather than a failed stage — so record whichever files actually exist. At
# least one must, or summarize.py would have exited non-zero above.
declare -a SUMMARY_ARTIFACTS=()
[ -f "$SUMMARY_FILE" ] && SUMMARY_ARTIFACTS+=("md=$SUMMARY_FILE")
[ -f "$SUMMARY_PDF" ] && SUMMARY_ARTIFACTS+=("pdf=$SUMMARY_PDF")
if [ "${#SUMMARY_ARTIFACTS[@]}" -eq 0 ]; then
  rs fail --run-dir "$RUN_DIR" --stage summarize \
     --error "summarize exited 0 but wrote neither $SUMMARY_FILE nor $SUMMARY_PDF"
  echo "[summarize] produced no output files" >&2
  exit 1
fi
mark_done summarize "${SUMMARY_ARTIFACTS[@]}" || exit 1

# Only ever after summarize is recorded as done — a run that dies here has to
# stay resumable against the frames it already paid for.
cleanup_frames

echo ""
echo "=================================================================="
echo "Run complete: $RUN_ID"
echo "=================================================================="
[ "$INPUT_TYPE" = "meeting" ] && echo "Recording:  $MP4_FILE"
echo "Transcript: ${TRANSCRIPT_BASE}.txt"
[ -f "$SUMMARY_FILE" ] && echo "Summary:    $SUMMARY_FILE"
[ -f "$SUMMARY_PDF" ] && echo "PDF:        $SUMMARY_PDF"
true
