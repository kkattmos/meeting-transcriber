#!/bin/bash
# The orchestrator: record -> transcribe -> summarize, for one input or many.
#
# Accepts Google Meet / Zoom URLs, YouTube URLs, and local media files, in any
# mix, in a single invocation. Each input becomes its own *run* with its own
# state, and runs execute concurrently up to --jobs.
#
#   ./pipeline.sh "https://www.youtube.com/watch?v=aaa" \
#                 "https://youtu.be/bbb" \
#                 "https://www.youtube.com/watch?v=ccc" --jobs 3
#
#   ./pipeline.sh --from-file links.txt --jobs 4
#   ./pipeline.sh "https://www.youtube.com/playlist?list=PL..." --playlist
#   ./pipeline.sh "https://meet.google.com/abc-defg-hij" --name "Weekly Standup"
#   ./pipeline.sh /path/to/recording.mp4 --language en
#
# RESUMING. State lives in /opt/meeting-bot/runs/<run_id>/. If a run fails
# partway, just run the same command again: it finds the unfinished run for
# that input and picks up at the first stage that isn't done, reusing the
# recording/transcript/frames that already succeeded. Or be explicit:
#
#   ./pipeline.sh --run-id <run_id>     resume that run
#   ./pipeline.sh --resume-last         resume the most recent run
#   ./pipeline.sh --resume-all          resume every unfinished run
#   ./pipeline.sh <input> --force       ignore prior state, start clean
#   ./pipeline.sh --list                show recent runs and their stages
#   ./pipeline.sh --status <run_id>     show one run in detail
#
# Options:
#   --name N            meeting name (single input only; otherwise derived)
#   --display-name D    name the bot shows in the meeting (default "Meeting Bot")
#   --language L        th (default), en, auto, or any AssemblyAI language code
#   --prompt P          a file in summarize/prompts/ (e.g. --prompt lecture-gemini)
#   --jobs N            how many inputs to process at once (default 2)
#   --from-file F       read inputs from a file, one per line, # for comments
#   --playlist          expand YouTube playlist URLs into their videos
#   --combine F         also write every summary into one file, in input order,
#                       shaped like a course chapter file (one Chapter line at
#                       the top, then each video's section). Per-run summaries
#                       are still written individually.
#
# The legacy positional form still works:
#   ./pipeline.sh <input> [name] [display_name] [language] [prompt]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"

MEETING_BOT_ROOT="${MEETING_BOT_ROOT:-/opt/meeting-bot}"
RUNS_DIR="$MEETING_BOT_ROOT/runs"
RUNSTATE="$SCRIPT_DIR/lib/runstate.py"

PYTHON_BIN="/opt/meeting-bot-venv/bin/python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"
rs() { "$PYTHON_BIN" "$RUNSTATE" "$@"; }

# --- Argument parsing --------------------------------------------------------
NAME=""
DISPLAY_NAME="${MEETING_BOT_DISPLAY_NAME:-Meeting Bot}"
LANGUAGE="${ASSEMBLYAI_LANGUAGE:-th}"
PROMPT_NAME="${SUMMARY_PROMPT:-}"
JOBS="${PIPELINE_JOBS:-2}"
FORCE=0
EXPAND_PLAYLIST=0
RESUME_LAST=0
RESUME_ALL=0
EXPLICIT_RUN_ID=""
FROM_FILE=""
COMBINE_FILE=""
declare -a POSITIONAL=()

usage() { sed -n '2,45p' "$0"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --name)         NAME="${2:-}"; shift 2 ;;
    --display-name) DISPLAY_NAME="${2:-}"; shift 2 ;;
    --language)     LANGUAGE="${2:-}"; shift 2 ;;
    --prompt)       PROMPT_NAME="${2:-}"; shift 2 ;;
    --jobs)         JOBS="${2:-2}"; shift 2 ;;
    --from-file)    FROM_FILE="${2:-}"; shift 2 ;;
    --combine)      COMBINE_FILE="${2:-}"; shift 2 ;;
    --run-id)       EXPLICIT_RUN_ID="${2:-}"; shift 2 ;;
    --playlist)     EXPAND_PLAYLIST=1; shift ;;
    --force)        FORCE=1; shift ;;
    --resume-last)  RESUME_LAST=1; shift ;;
    --resume-all)   RESUME_ALL=1; shift ;;
    --list)
      rs list --root "$RUNS_DIR"
      echo ""
      echo "Legend: +done  !failed  >running  .pending"
      echo "Resume one with: ./pipeline.sh --run-id <RUN ID>"
      exit 0
      ;;
    --status)
      [ -n "${2:-}" ] || { echo "--status needs a run id" >&2; exit 1; }
      rs show --run-dir "$RUNS_DIR/$2"
      exit $?
      ;;
    -h|--help) usage; exit 0 ;;
    --*) echo "Unknown option: $1 (try --help)" >&2; exit 1 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done

# --- Input classification ----------------------------------------------------
classify_input() {
  local value="$1"
  if [ -f "$value" ]; then
    echo "local_file"
  elif echo "$value" | grep -qE '(meet\.google\.com/|^https?://[^/]*zoom\.us/)'; then
    echo "meeting"
  elif echo "$value" | grep -qE '(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/playlist\?list=)'; then
    echo "youtube"
  else
    echo "unknown"
  fi
}

# "Is this argument meant to be an input at all?" — used to keep the legacy
# positional form (<input> <name> <display> <lang> <prompt>) working alongside
# the new multi-input form. A URL or an existing path is an input; a bare word
# like "Weekly Standup" is not.
looks_like_input() {
  case "$1" in
    http://*|https://*) return 0 ;;
  esac
  [ -f "$1" ]
}

declare -a INPUTS=()
declare -a LEGACY_EXTRAS=()
# Guard the empty case explicitly: "${POSITIONAL[@]:-}" on an empty array
# expands to a single empty string, which would be counted as a legacy
# positional and make `--from-file` with no positionals look ambiguous.
if [ "${#POSITIONAL[@]}" -gt 0 ]; then
  for arg in "${POSITIONAL[@]}"; do
    # Empty positionals are placeholders in the legacy form
    # (`pipeline.sh <url> "" "" en`), so they hold their slot.
    if [ -z "$arg" ]; then
      LEGACY_EXTRAS+=("")
    elif looks_like_input "$arg"; then
      INPUTS+=("$arg")
    else
      LEGACY_EXTRAS+=("$arg")
    fi
  done
fi

# Pull inputs out of a file too. Blank lines and #-comments are skipped, so a
# link list can be annotated.
if [ -n "$FROM_FILE" ]; then
  if [ ! -f "$FROM_FILE" ]; then
    echo "ERROR: --from-file: no such file: $FROM_FILE" >&2
    exit 1
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(echo "$line" | tr -d '\r' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    INPUTS+=("$line")
  done < "$FROM_FILE"
fi

# Legacy positional mapping, only when it's unambiguous (exactly one input).
if [ "${#INPUTS[@]}" -eq 1 ] && [ "${#LEGACY_EXTRAS[@]}" -gt 0 ]; then
  [ -n "${LEGACY_EXTRAS[0]:-}" ] && [ -z "$NAME" ] && NAME="${LEGACY_EXTRAS[0]}"
  [ -n "${LEGACY_EXTRAS[1]:-}" ] && DISPLAY_NAME="${LEGACY_EXTRAS[1]}"
  [ -n "${LEGACY_EXTRAS[2]:-}" ] && LANGUAGE="${LEGACY_EXTRAS[2]}"
  [ -n "${LEGACY_EXTRAS[3]:-}" ] && [ -z "$PROMPT_NAME" ] && PROMPT_NAME="${LEGACY_EXTRAS[3]}"
elif [ "${#INPUTS[@]}" -gt 1 ]; then
  # Only *non-empty* leftovers are ambiguous; a bare "" carries no meaning
  # once there's more than one input.
  declare -a REAL_EXTRAS=()
  for extra in "${LEGACY_EXTRAS[@]:-}"; do
    [ -n "$extra" ] && REAL_EXTRAS+=("$extra")
  done
  if [ "${#REAL_EXTRAS[@]}" -gt 0 ]; then
    echo "ERROR: don't mix multiple inputs with the legacy positional form." >&2
    echo "  Unrecognized arguments: ${REAL_EXTRAS[*]}" >&2
    echo "  With several inputs, use the flags: --name, --language, --prompt, ..." >&2
    exit 1
  fi
fi

if [ "${#INPUTS[@]}" -gt 1 ] && [ -n "$NAME" ]; then
  echo "WARNING: --name is ignored with multiple inputs; names are derived per input."
  NAME=""
fi

# --- Playlist expansion ------------------------------------------------------
# Off by default: the common case is a watch URL that happens to carry a &list=
# parameter, and silently transcribing 200 videos because of it would be rude.
if [ "$EXPAND_PLAYLIST" -eq 1 ]; then
  declare -a EXPANDED=()
  for input in "${INPUTS[@]:-}"; do
    if echo "$input" | grep -qE '[?&]list='; then
      echo "==> Expanding playlist: $input"
      if ! command -v yt-dlp >/dev/null 2>&1; then
        echo "ERROR: --playlist needs yt-dlp. Run ./setup.sh first." >&2
        exit 1
      fi
      while IFS= read -r vid; do
        [ -n "$vid" ] && EXPANDED+=("https://www.youtube.com/watch?v=$vid")
      done < <(yt-dlp --flat-playlist --print id "$input" 2>/dev/null)
    else
      EXPANDED+=("$input")
    fi
  done
  INPUTS=("${EXPANDED[@]:-}")
  echo "==> ${#INPUTS[@]} video(s) after expansion"
fi

# --- Resolve which runs to execute -------------------------------------------
# Each entry is "<run_dir>" — either resumed or freshly created.
declare -a RUN_DIRS=()

sanitize() { echo "$1" | tr ' ' '_' | tr -cd 'A-Za-z0-9_-'; }

derive_safe_name() {
  local input="$1" kind="$2"
  case "$kind" in
    youtube)
      local vid
      vid=$(echo "$input" | sed -nE 's#.*(youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{6,}).*#\2#p')
      [ -z "$vid" ] && vid="video"
      echo "yt_${vid}"
      ;;
    local_file)
      local base
      base="$(basename "$input")"
      sanitize "${base%.*}"
      ;;
    *)
      sanitize "${NAME:-meeting}"
      ;;
  esac
}

if [ -n "$EXPLICIT_RUN_ID" ]; then
  if [ ! -d "$RUNS_DIR/$EXPLICIT_RUN_ID" ]; then
    echo "ERROR: no such run: $EXPLICIT_RUN_ID" >&2
    echo "  See them with: ./pipeline.sh --list" >&2
    exit 1
  fi
  RUN_DIRS+=("$RUNS_DIR/$EXPLICIT_RUN_ID")
elif [ "$RESUME_LAST" -eq 1 ]; then
  last="$(rs latest --root "$RUNS_DIR")" || {
    echo "ERROR: no runs found under $RUNS_DIR" >&2; exit 1; }
  RUN_DIRS+=("$RUNS_DIR/$last")
elif [ "$RESUME_ALL" -eq 1 ]; then
  for run_dir in "$RUNS_DIR"/*/; do
    [ -f "${run_dir}state.json" ] || continue
    status="$(rs status --run-dir "${run_dir%/}" --stage summarize)"
    [ "$status" = "done" ] && continue
    RUN_DIRS+=("${run_dir%/}")
  done
  if [ "${#RUN_DIRS[@]}" -eq 0 ]; then
    echo "==> Nothing to resume: every run has finished summarizing."
    exit 0
  fi
  echo "==> Resuming ${#RUN_DIRS[@]} unfinished run(s)"
else
  if [ "${#INPUTS[@]}" -eq 0 ]; then
    # Arguments were given, but none of them look like an input. Say which,
    # rather than dumping the usage text and leaving the user to spot the typo.
    declare -a UNRECOGNIZED=()
    for extra in "${LEGACY_EXTRAS[@]:-}"; do
      [ -n "$extra" ] && UNRECOGNIZED+=("$extra")
    done
    if [ "${#UNRECOGNIZED[@]}" -gt 0 ]; then
      echo "ERROR: unrecognized input: ${UNRECOGNIZED[0]}" >&2
      echo "  Expected a Google Meet or Zoom URL, a YouTube URL, or a path to" >&2
      echo "  a local media file that exists on disk." >&2
      echo "  (A local path is only recognized if the file is actually there —" >&2
      echo "   check for a typo in the path.)" >&2
      exit 1
    fi
    usage
    exit 1
  fi
  for input in "${INPUTS[@]}"; do
    kind="$(classify_input "$input")"
    if [ "$kind" = "unknown" ]; then
      echo "ERROR: unrecognized input: $input" >&2
      echo "  Expected a Google Meet or Zoom URL, a YouTube URL, or a path to" >&2
      echo "  a local media file that exists." >&2
      exit 1
    fi

    # Auto-resume: an unfinished run for this exact input gets picked up rather
    # than duplicated. --force always starts a clean run instead.
    existing=""
    if [ "$FORCE" -eq 0 ]; then
      existing="$(rs find --root "$RUNS_DIR" --input "$input" --incomplete 2>/dev/null || true)"
    fi

    if [ -n "$existing" ]; then
      echo "==> Resuming unfinished run for $input"
      echo "    run id: $existing   (use --force to start over instead)"
      run_dir="$RUNS_DIR/$existing"
    else
      safe="$(derive_safe_name "$input" "$kind")"
      [ -z "$safe" ] && safe="meeting"
      run_dir="$RUNS_DIR/${safe}_$(date +%Y%m%d_%H%M%S)"
      # Two inputs starting in the same second would otherwise share a run dir.
      suffix=1
      while [ -d "$run_dir" ]; do
        run_dir="$RUNS_DIR/${safe}_$(date +%Y%m%d_%H%M%S)_$suffix"
        suffix=$((suffix + 1))
      done
      rs init --run-dir "$run_dir" \
        --input "$input" --input-type "$kind" \
        --name "${NAME:-$safe}" --safe-name "$safe" \
        --language "$LANGUAGE" --prompt "$PROMPT_NAME" \
        --display-name "$DISPLAY_NAME"
    fi
    RUN_DIRS+=("$run_dir")
  done
fi

# --- Execute -----------------------------------------------------------------
mkdir -p "$RUNS_DIR"
TOTAL="${#RUN_DIRS[@]}"
echo ""
echo "==> $TOTAL run(s), up to $JOBS at a time"

RESULT_DIR="$(mktemp -d)"
trap 'rm -rf "$RESULT_DIR"' EXIT

launch() {
  local run_dir="$1"
  local run_id
  run_id="$(basename "$run_dir")"
  local args=(--run-dir "$run_dir")
  [ "$FORCE" -eq 1 ] && args+=(--force)

  (
    if [ "$TOTAL" -gt 1 ]; then
      # Prefix every line so concurrent runs stay readable. awk rather than
      # `sed -u` because busybox sed has no unbuffered mode.
      bash "$SCRIPT_DIR/lib/run_one.sh" "${args[@]}" 2>&1 \
        | awk -v r="$run_id" '{print r " | " $0; fflush()}'
      echo "${PIPESTATUS[0]}" > "$RESULT_DIR/$run_id"
    else
      bash "$SCRIPT_DIR/lib/run_one.sh" "${args[@]}"
      echo "$?" > "$RESULT_DIR/$run_id"
    fi
  ) &
}

for run_dir in "${RUN_DIRS[@]}"; do
  # Simple slot gate: wait until fewer than $JOBS children are running.
  while [ "$(jobs -rp | wc -l)" -ge "$JOBS" ]; do
    sleep 1
  done
  launch "$run_dir"
done
wait

# --- Report ------------------------------------------------------------------
echo ""
echo "=================================================================="
echo "All runs finished"
echo "=================================================================="
FAILED=0
for run_dir in "${RUN_DIRS[@]}"; do
  run_id="$(basename "$run_dir")"
  rc="$(cat "$RESULT_DIR/$run_id" 2>/dev/null || echo "?")"
  if [ "$rc" = "0" ]; then
    summary="$(rs get --run-dir "$run_dir" --key stages.summarize.artifacts.md 2>/dev/null || true)"
    echo "  OK    $run_id"
    [ -n "$summary" ] && echo "        -> $summary"
  else
    FAILED=$((FAILED + 1))
    echo "  FAIL  $run_id  (exit $rc)"
    echo "        details:  ./pipeline.sh --status $run_id"
    echo "        resume:   ./pipeline.sh --run-id $run_id"
  fi
done

# --- Combined output ---------------------------------------------------------
# Concatenate in INPUT order, not completion order — runs finish out of order
# when several run at once, and a chapter file has to follow the lecture order.
if [ -n "$COMBINE_FILE" ]; then
  declare -a SUMMARY_PATHS=()
  for run_dir in "${RUN_DIRS[@]}"; do
    md="$(rs get --run-dir "$run_dir" --key stages.summarize.artifacts.md 2>/dev/null || true)"
    [ -n "$md" ] && [ -f "$md" ] && SUMMARY_PATHS+=("$md")
  done
  if [ "${#SUMMARY_PATHS[@]}" -eq 0 ]; then
    echo ""
    echo "==> --combine: no summaries were produced, nothing to combine."
  else
    mkdir -p "$(dirname "$COMBINE_FILE")"
    "$PYTHON_BIN" "$SCRIPT_DIR/summarize/document.py" combine \
      --output "$COMBINE_FILE" "${SUMMARY_PATHS[@]}"
  fi
fi

if [ "$FAILED" -gt 0 ]; then
  echo ""
  echo "$FAILED of $TOTAL run(s) failed. Everything that succeeded is saved —"
  echo "resuming re-runs only the stages that didn't finish."
  exit 1
fi
