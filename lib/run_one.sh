#!/bin/bash
# Execute the stage DAG for ONE input. pipeline.sh calls this once per input,
# possibly several at a time; you can also call it directly to resume a single
# run without touching the others.
#
# Usage:
#   lib/run_one.sh --run-dir DIR [--force] [--skip-summarize]
#
# --skip-summarize stops after transcribe and frames. pipeline.sh passes it to
# every member of a --combine set: their summarize stage is left pending on
# purpose, because the combine run (input_type "combine", below) is what
# summarizes them — all of them, as one document.
#
# The clip window, like the resources list, is read back out of state.json
# rather than passed: a resume must cut the same window the first attempt did.
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
#
# --clip adds one more stage, `clip`, which cuts the requested window out of
# the media with ffmpeg. Everything after it is handed the CLIP and never
# learns a window existed — which is what makes the output timestamps
# clip-relative without an offset being threaded through four stages.
# It sits wherever the media first exists:
#
#     local file / kaltura:  ... fetch_video ──> clip ──┬─> transcribe ─┐
#                                                       └─> frames ─────┴─> summarize
#     youtube:               fetch_video ──> clip ──> frames   (transcribe runs
#                                                               off captions and
#                                                               is windowed by
#                                                               transcribe.sh)
#
# A combine run is the odd one out. It has no media and only one stage:
#
#     member 1 (transcribe, frames) ─┐
#     member 2 (transcribe, frames) ─┼─> summarize   (summarize.py --parts)
#     member N (transcribe, frames) ─┘
#
# It reads its members' transcripts and frame manifests straight out of their
# state.json files, so it runs after they have finished — pipeline.sh waits —
# and a `--run-id <combine id>` resume re-runs any member whose artifacts have
# gone missing before it summarizes.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# shellcheck disable=SC1091
. "$ROOT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$ROOT_DIR/lib/paths.sh"

RUNSTATE="$SCRIPT_DIR/runstate.py"
# summarize.py's exit status for "the Claude usage window is exhausted; try
# again after it resets" (EX_TEMPFAIL). Passed up unchanged so pipeline.sh
# can report the run as paused rather than failed.
EXIT_PAUSED=75
SLOTQUEUE="$SCRIPT_DIR/slotqueue.py"

PYTHON_BIN="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}/bin/python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"

RUN_DIR=""
FORCE=0
SKIP_SUMMARIZE=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --run-dir) RUN_DIR="${2:-}"; shift 2 ;;
    --force)   FORCE=1; shift ;;
    --skip-summarize) SKIP_SUMMARIZE=1; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
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
# The clip window, replayed from state.json on every attempt exactly like the
# resources list — a resume has to cut the same window the first attempt did or
# it would summarize a different video onto the same artifact paths.
CLIP="$(cfg clip)"
# Set on a member of a --combine set: the combine run that summarizes it.
COMBINED_INTO="$(cfg combined_into)"
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
# Lives in the run dir, beside the YouTube/Kaltura download it is cut from,
# because it is derived data with the same lifetime: cheap to remake from the
# source, and swept with the rest of the run dir.
CLIP_FILE="$RUN_DIR/clip.mp4"
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

  if [ "$rc" -eq "$EXIT_PAUSED" ]; then
    # summarize.py ran out of Claude usage window and gave up waiting. Not
    # broken: the stage is failed (so a resume re-runs it) and the reset
    # time is already in state.json, where --resume-all reads it.
    rs fail --run-dir "$RUN_DIR" --stage "$stage" \
       --error "$(tail -n 5 "$log" 2>/dev/null)"
    local resumes_at
    resumes_at="$(rs get --run-dir "$RUN_DIR" --key "stages.$stage.rate_limited.resets_at_iso" 2>/dev/null || true)"
    echo "[$stage] PAUSED — Claude usage window exhausted; resumable after ${resumes_at:-the reset}" >&2
    return "$rc"
  fi
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

# --- A combine run: one summarize stage over several members' outputs -------
# Lives up here, ahead of the per-input stages, because none of them apply:
# there is no media to fetch, cut, transcribe or sample. Everything the
# summarizer needs is already on disk in the member runs.

do_summarize_combined() {
  local parts_file="$1"
  local args=(
    "$ROOT_DIR/summarize/summarize.py"
    --parts "$parts_file"
    "$COMBINE_MD"
    --run-id "$RUN_ID"
  )
  if [ -n "$COMBINE_PDF" ]; then
    args+=(--pdf-out "$COMBINE_PDF")
  else
    args+=(--no-pdf)
  fi
  [ -n "$PROMPT_NAME" ] && args+=(--prompt "$PROMPT_NAME")
  local spec
  for spec in "${RESOURCE_SPECS[@]:-}"; do
    [ -n "$spec" ] && args+=(--resources "$spec")
  done
  "$PYTHON_BIN" "${args[@]}"
}

# The members' frames, swept once the combined PDF exists. Same rules as
# cleanup_frames below — KEEP_FRAMES=1 keeps them, and so does a PDF that was
# asked for and did not render, since re-rendering it needs them.
cleanup_member_frames() {
  case "$(printf '%s' "${KEEP_FRAMES:-0}" | tr 'A-Z' 'a-z')" in
    1|true|yes|on) return 0 ;;
  esac
  [ -n "${FRAMES_DIR:-}" ] || return 0
  if [ -n "$COMBINE_PDF" ] && [ ! -f "$COMBINE_PDF" ]; then
    echo "[frames] kept — the combined PDF did not render, and re-rendering it needs them"
    return 0
  fi
  local member member_frames
  for member in "${MEMBERS[@]}"; do
    member_frames="$FRAMES_DIR/$member"
    [ -n "$member" ] && [ "$member_frames" != "$FRAMES_DIR" ] || continue
    [ -d "$member_frames" ] || continue
    rm -rf "$member_frames"
    rs cleaned --run-dir "$RUNS_DIR/$member" --stage frames || true
  done
  echo "[frames] members' frames removed (KEEP_FRAMES=1 to keep them)"
}

run_combine() {
  local member member_dir
  echo "=================================================================="
  echo "Combine run: $RUN_ID"
  echo "  members:  ${MEMBERS[*]}"
  echo "  output:   $COMBINE_MD"
  [ -n "$COMBINE_PDF" ] && echo "  pdf:      $COMBINE_PDF"
  echo "  prompt:   ${PROMPT_NAME:-(default)}"
  echo "=================================================================="

  if [ "$(stage_status summarize)" = "done" ]; then
    echo "[summarize] already done — skipping (use --force to redo)"
    echo "Summary:    $COMBINE_MD"
    [ -n "$COMBINE_PDF" ] && [ -f "$COMBINE_PDF" ] && echo "PDF:        $COMBINE_PDF"
    return 0
  fi

  # Every member has to have its transcript and its frames on disk. Normally
  # they do — pipeline.sh just ran them. On a `--run-id` resume after the
  # frames were swept (or a member was never finished) they may not, and the
  # member is the thing that knows how to make them: run it. Frames that were
  # swept on purpose sit at `done` with no artifacts, so the stage is reset
  # first or the member would skip it.
  for member in "${MEMBERS[@]}"; do
    member_dir="$RUNS_DIR/$member"
    if [ ! -f "$member_dir/state.json" ]; then
      echo "[summarize] member run $member does not exist under $RUNS_DIR" >&2
      rs fail --run-dir "$RUN_DIR" --stage summarize \
        --error "member run $member is missing"
      return 1
    fi
    local manifest
    manifest="$(rs get --run-dir "$member_dir" --key stages.frames.artifacts.manifest 2>/dev/null || true)"
    if [ -z "$manifest" ] || [ ! -f "$manifest" ]; then
      echo "==> $member: frames were swept or never extracted — extracting them"
      rs reset --run-dir "$member_dir" --stage frames
    fi
    if [ "$(rs status --run-dir "$member_dir" --stage transcribe)" != "done" ] \
       || [ "$(rs status --run-dir "$member_dir" --stage frames)" != "done" ]; then
      echo ""
      echo "==> $member: finishing transcribe/frames before the combined summary"
      if ! bash "$SCRIPT_DIR/run_one.sh" --run-dir "$member_dir" --skip-summarize; then
        echo "[summarize] member $member could not be completed" >&2
        rs fail --run-dir "$RUN_DIR" --stage summarize \
          --error "member run $member failed; see $member_dir/logs"
        return 1
      fi
    fi
  done

  # parts.json is rebuilt on every attempt rather than cached: it is a view
  # of the members' state, and a member re-run above may have moved a path.
  local parts_file="$RUN_DIR/parts.json"
  if ! "$PYTHON_BIN" "$SCRIPT_DIR/combine.py" parts \
         --runs-dir "$RUNS_DIR" --out "$parts_file" "${MEMBERS[@]}"; then
    rs fail --run-dir "$RUN_DIR" --stage summarize \
      --error "could not assemble parts.json from the member runs"
    return 1
  fi

  echo ""
  run_stage summarize do_summarize_combined "$parts_file" || return $?
  local -a artifacts=()
  [ -f "$COMBINE_MD" ] && artifacts+=("md=$COMBINE_MD")
  [ -n "$COMBINE_PDF" ] && [ -f "$COMBINE_PDF" ] && artifacts+=("pdf=$COMBINE_PDF")
  if [ "${#artifacts[@]}" -eq 0 ]; then
    rs fail --run-dir "$RUN_DIR" --stage summarize \
      --error "summarize exited 0 but wrote neither $COMBINE_MD nor ${COMBINE_PDF:-a PDF}"
    echo "[summarize] produced no output files" >&2
    return 1
  fi
  mark_done summarize "${artifacts[@]}" || return 1
  cleanup_member_frames

  echo ""
  echo "=================================================================="
  echo "Combine run complete: $RUN_ID"
  echo "=================================================================="
  echo "Summary:    $COMBINE_MD"
  [ -n "$COMBINE_PDF" ] && [ -f "$COMBINE_PDF" ] && echo "PDF:        $COMBINE_PDF"
  return 0
}

if [ "$INPUT_TYPE" = "combine" ]; then
  RUNS_DIR="$(dirname "$RUN_DIR")"
  declare -a MEMBERS=()
  while IFS= read -r _m; do
    [ -n "$_m" ] && MEMBERS+=("$_m")
  done < <(rs get --run-dir "$RUN_DIR" --key members 2>/dev/null || true)
  COMBINE_MD="$(cfg output_md)"
  COMBINE_PDF="$(cfg output_pdf)"
  if [ "${#MEMBERS[@]}" -eq 0 ] || [ -z "$COMBINE_MD" ]; then
    echo "run_one.sh: combine run $RUN_ID has no members or no output path in state.json" >&2
    exit 2
  fi
  rc=0
  run_combine || rc=$?
  [ "$rc" -eq 0 ] && exit 0
  echo "    Resume with:  ./pipeline.sh --run-id $RUN_ID" >&2
  [ "$rc" -eq "$EXIT_PAUSED" ] && exit "$EXIT_PAUSED"
  exit 1
fi

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

# Cut the window out of whatever media this run has. Everything downstream —
# transcribe, frames, summarize — is then handed the clip and never learns a
# window existed, which is what makes the output timestamps clip-relative
# without a single offset being threaded through four stages.
do_clip() {
  local src="$1"
  "$PYTHON_BIN" "$SCRIPT_DIR/clip.py" cut "$src" "$CLIP_FILE" "$CLIP"
}

do_transcribe() {
  local src="$1"
  local args=("$src" "$SAFE_NAME" "$LANGUAGE" --out-base "$TRANSCRIPT_BASE")
  # Only the caption backends act on this. When AssemblyAI is used the media
  # it receives has already been cut, so applying the window again would
  # take a second slice out of the first one.
  [ -n "$CLIP" ] && args+=(--clip-captions "$CLIP")
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
  # Recorded in the document's provenance comment. Without it a reader has no
  # way to tell that "0:00:00" in this summary is 00:05:00 in the source.
  [ -n "$CLIP" ] && args+=(--clip "$CLIP")
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
# The media this run started from, before any window was cut out of it.
resolve_source_video() {
  case "$INPUT_TYPE" in
    local_file) VIDEO_FILE="$INPUT" ;;
    meeting)    VIDEO_FILE="$MP4_FILE" ;;
    youtube|kaltura)
      VIDEO_FILE="$(rs get --run-dir "$RUN_DIR" --key stages.fetch_video.artifacts.video 2>/dev/null || true)"
      if [ -z "$VIDEO_FILE" ]; then
        VIDEO_FILE="$(ls -1 "$RUN_DIR"/video.* 2>/dev/null | grep -v '\.part$' | head -n 1)"
      fi
      ;;
  esac
}

# The media every stage after the cut should use. On a clipped run that is the
# clip; on every other run it is the source, unchanged. Nothing downstream
# branches on the window — this function is the only place that knows.
resolve_video() {
  if [ -n "$CLIP" ] && [ -f "$CLIP_FILE" ]; then
    VIDEO_FILE="$CLIP_FILE"
    return 0
  fi
  resolve_source_video
}

echo "=================================================================="
echo "Run: $RUN_ID"
echo "  input:    $INPUT ($INPUT_TYPE)"
echo "  language: $LANGUAGE   prompt: ${PROMPT_NAME:-(default)}"
[ -n "$CLIP" ] && echo "  clip:     $CLIP  (output timestamps are relative to it)"
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

# The cut, shared by both callers below exactly like ensure_video_fetched, and
# idempotent for the same reason: on the Kaltura and local-file paths it runs
# ahead of both branches (transcribe needs the clipped audio), while on the
# YouTube path there is nothing to transcribe from the media at all, so it runs
# inside the frames branch right after the download. Calling it twice must be
# free.
#
# A no-op when this run has no window, so the callers stay unconditional.
ensure_video_clipped() {
  [ -n "$CLIP" ] || return 0
  if [ "$(stage_status clip)" = "done" ]; then
    echo "[clip] already done — skipping"
    return 0
  fi
  resolve_source_video
  if [ -z "${VIDEO_FILE:-}" ] || [ ! -f "$VIDEO_FILE" ]; then
    rs fail --run-dir "$RUN_DIR" --stage clip \
      --error "no source media at '${VIDEO_FILE:-}' to cut $CLIP out of"
    echo "[clip] no source media at '${VIDEO_FILE:-}' — cannot cut $CLIP" >&2
    return 1
  fi
  # Never cut the clip out of itself. resolve_source_video can't return
  # CLIP_FILE, but a stale half-written one from a killed run can still be
  # sitting there, and ffmpeg would happily read it as the source.
  # clip.part.mp4, not clip.mp4.part — ffmpeg picks its muxer from the
  # extension, so the partial keeps it. See lib/clip.py.
  rm -f "$CLIP_FILE" "$RUN_DIR/clip.part.mp4"
  echo "==> Cutting $CLIP out of $VIDEO_FILE"
  run_stage clip do_clip "$VIDEO_FILE" || return 1
  mark_done clip "video=$CLIP_FILE" || return 1
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
  # A finished frames stage whose manifest is gone was swept on purpose after
  # a summary (cleanup_frames below, or a combine run's). If nothing here is
  # going to summarize, that is fine and the stage stays done — mark_done
  # would otherwise refuse the missing manifest and fail a run that has
  # nothing left to do. If this run IS about to summarize, the frames are
  # needed again, and re-extracting them is cheap (the video outlives them).
  if [ "$(stage_status frames)" = "done" ]; then
    if [ -f "$RUN_FRAMES_DIR/manifest.json" ] || [ "$SKIP_SUMMARIZE" -eq 1 ]; then
      echo "[frames] already done — skipping"
      return 0
    fi
    echo "[frames] swept after the last summary — extracting them again"
    rs reset --run-dir "$RUN_DIR" --stage frames
  fi
  if [ "$INPUT_TYPE" = "youtube" ]; then
    ensure_video_fetched || return 1
    # Only here: the YouTube transcribe branch runs off captions, so it is
    # already finished with the window (transcribe.sh applies it to the
    # segments) and never waits on this.
    ensure_video_clipped || return 1
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

# The cut, for every input whose TRANSCRIBE branch reads the media: a local
# file, and Kaltura when the entry has no captions. Both branches must see the
# same clip, so it happens before either starts rather than inside one of them.
#
# YouTube is the exception and is handled inside branch_frames: its transcript
# comes from captions and never opens the media at all, so making transcription
# wait for a download and an ffmpeg pass would serialize two stages that have
# nothing to say to each other.
if [ -n "$CLIP" ] && [ "$INPUT_TYPE" != "youtube" ]; then
  echo ""
  if ! ensure_video_clipped; then
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
if [ "$SKIP_SUMMARIZE" -eq 1 ]; then
  # A member of a --combine set. Its frames stay for the combine run to read;
  # that run sweeps them once the combined PDF is written.
  echo ""
  echo "[summarize] skipped — this run is summarized together with the rest"
  echo "            of its --combine set (run ${COMBINED_INTO:-<combine>})"
  echo ""
  echo "=================================================================="
  echo "Run ready for the combined summary: $RUN_ID"
  echo "=================================================================="
  [ "$INPUT_TYPE" = "meeting" ] && echo "Recording:  $MP4_FILE"
  echo "Transcript: ${TRANSCRIPT_BASE}.txt"
  echo "Frames:     $RUN_FRAMES_DIR/manifest.json"
  exit 0
fi

resolve_video
echo ""
rc=0
run_stage summarize do_summarize "$VIDEO_FILE" || rc=$?
if [ "$rc" -ne 0 ]; then
  echo "    Resume with:  ./pipeline.sh --run-id $RUN_ID" >&2
  [ "$rc" -eq "$EXIT_PAUSED" ] && exit "$EXIT_PAUSED"
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
