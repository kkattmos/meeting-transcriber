#!/bin/bash
# The orchestrator: record -> transcribe -> summarize, for one input or many.
#
# Accepts Google Meet / Zoom URLs, YouTube URLs, Kaltura embeds and local media
# files, in any mix, in a single invocation. Each input becomes its own *run*
# with its own state, and runs execute concurrently up to --jobs.
#
# A Kaltura lecture can be given either as the whole <iframe> tag copied out of
# the LMS or as just its src URL — quote it, since the tag contains spaces:
#
#   ./pipeline.sh '<iframe id="kaltura_player" src="https://cdnapisec.kaltura.com/p/123/...entry_id=1_abcdefgh"></iframe>'
#   ./pipeline.sh "https://cdnapisec.kaltura.com/p/123/embedPlaykitJs/uiconf_id/456?entry_id=1_abcdefgh"
#
#   ./pipeline.sh "https://www.youtube.com/watch?v=aaa" \
#                 "https://youtu.be/bbb" \
#                 "https://www.youtube.com/watch?v=ccc" --jobs 3
#
#   ./pipeline.sh --from-file links.txt --jobs 4
#   ./pipeline.sh "https://www.youtube.com/playlist?list=PL..." --playlist
#   ./pipeline.sh "https://meet.google.com/abc-defg-hij" --name "Weekly Standup"
#   ./pipeline.sh /path/to/recording.mp4 --language en
#   ./pipeline.sh "https://youtu.be/bbb" --clip 00:05:00-01:30:00
#
# RESUMING. State lives in /opt/meeting-bot/runs/<run_id>/. If a run fails
# partway, just run the same command again: it finds the unfinished run for
# that input and picks up at the first stage that isn't done, reusing the
# recording/transcript/frames that already succeeded. Or be explicit:
#
#   ./pipeline.sh --run-id <run_id>     resume that run
#   ./pipeline.sh --resume-last         resume the most recent run
#   ./pipeline.sh --resume-all          resume every unfinished run (a run
#                                       paused on the Claude usage window is
#                                       left alone until the window resets)
#   ./pipeline.sh <input> --force       ignore prior state, start clean
#   ./pipeline.sh --list                show recent runs and their stages
#   ./pipeline.sh --status <run_id>     show one run in detail
#
# Options:
#   --name N            meeting name (single input only; otherwise derived)
#   --display-name D    name the bot shows in the meeting (default "Meeting Bot")
#   --language L        th (default), en, auto, or any AssemblyAI language code
#   --prompt P          a file in summarize/prompts/ (e.g. --prompt lecture-claude)
#   --resources SPEC    slides / notes for this session, as a GitHub repo
#                       (optionally @branch, or a /tree/<branch>/<subdir> URL)
#                       or a local file or folder. Repeatable. Their text is
#                       given to the summarizer as reference material and their
#                       slide images are embedded in the PDF.
#   --clip W            summarize only part of the video: --clip 00:05:00-01:30:00
#                       (also MM:SS, bare seconds, or an open end: 00:05:00-).
#                       The media is cut to the window before transcription, so
#                       AssemblyAI only bills those minutes — and every
#                       timestamp in the output is relative to the CLIP, not to
#                       the source video. A clipped run gets its own run id, so
#                       it never overwrites a full summary of the same input.
#                       Not accepted for a live meeting URL: there is no source
#                       to clip.
#                       For one window per input, append #t=W to that input
#                       instead — it overrides --clip for that one:
#                         ./pipeline.sh "https://youtu.be/aaa#t=00:05:00-01:30:00" \
#                                       "https://youtu.be/bbb"
#                       which keeps everything in one invocation, and so in one
#                       --combine document.
#   --jobs N            how many inputs to process at once (default 2)
#   --from-file F       read inputs from a file, one per line, # for comments
#   --playlist          expand YouTube playlist URLs into their videos
#   --combine F         summarize ALL the inputs together, as one lecture, into
#                       one document F (and a PDF beside it). Each input is
#                       still transcribed and frame-sampled on its own, but
#                       none of them gets an individual summary: the model
#                       reads every transcript, in input order, and writes a
#                       single study guide. Timestamps stay relative to the
#                       video they belong to, and the document says which.
#                       The combined summary is a run of its own
#                       (combine_<n>x_<hash>_<time>) and resumes like any other.
#   --combine-pdf F     where the combined PDF goes (default: --combine's path
#                       with a .pdf extension)
#   --no-combine-pdf    write only the combined markdown
#
# The legacy positional form still works:
#   ./pipeline.sh <input> [name] [display_name] [language] [prompt]
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib/paths.sh"

RUNS_DIR="$MEETING_BOT_ROOT/runs"
RUNSTATE="$SCRIPT_DIR/lib/runstate.py"

PYTHON_BIN="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}/bin/python3"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="python3"
rs() { "$PYTHON_BIN" "$RUNSTATE" "$@"; }
# run_one.sh's exit status for a summarize stage that paused on an exhausted
# Claude usage window (EX_TEMPFAIL). Reported as PAUSED, not FAIL.
EXIT_PAUSED=75

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
COMBINE_PDF=""
COMBINE_WANT_PDF=1
CLIP_SPEC=""
declare -a POSITIONAL=()
declare -a RESOURCE_SPECS=()
# RESOURCES in .env is the default for every run; --resources adds to it.
if [ -n "${RESOURCES:-}" ]; then
  while IFS= read -r _spec; do
    _spec="$(printf '%s' "$_spec" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -n "$_spec" ] && RESOURCE_SPECS+=("$_spec")
  done < <(printf '%s\n' "$RESOURCES" | tr ',' '\n')
fi

usage() { sed -n '2,64p' "$0"; }

while [ "$#" -gt 0 ]; do
  case "$1" in
    --name)         NAME="${2:-}"; shift 2 ;;
    --display-name) DISPLAY_NAME="${2:-}"; shift 2 ;;
    --language)     LANGUAGE="${2:-}"; shift 2 ;;
    --prompt)       PROMPT_NAME="${2:-}"; shift 2 ;;
    --jobs)         JOBS="${2:-2}"; shift 2 ;;
    --clip)         CLIP_SPEC="${2:-}"; shift 2 ;;
    --from-file)    FROM_FILE="${2:-}"; shift 2 ;;
    --combine)      COMBINE_FILE="${2:-}"; shift 2 ;;
    --combine-pdf)  COMBINE_PDF="${2:-}"; shift 2 ;;
    --no-combine-pdf) COMBINE_WANT_PDF=0; shift ;;
    --run-id)       EXPLICIT_RUN_ID="${2:-}"; shift 2 ;;
    --resources)
      [ -n "${2:-}" ] || { echo "--resources needs a value" >&2; exit 1; }
      RESOURCE_SPECS+=("$2"); shift 2 ;;
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

# --- The clip window, settled before anything is classified ------------------
# Parsed here and only here, so a typo costs nothing: an unreadable window must
# fail in the first second, not after a download and an AssemblyAI charge. The
# canonical LABEL (not the raw spec) is what gets stored and matched on, so
# "5:00-90:00" and "00:05:00-01:30:00" resume into the same run instead of
# quietly making two.
CLIP_LABEL=""
CLIP_TOKEN=""
if [ -n "$CLIP_SPEC" ]; then
  CLIP_JSON="$("$PYTHON_BIN" "$SCRIPT_DIR/lib/clip.py" parse "$CLIP_SPEC")" || exit 1
  CLIP_LABEL="$(printf '%s' "$CLIP_JSON" | sed -nE 's/.*"label": "([^"]*)".*/\1/p')"
  CLIP_TOKEN="$(printf '%s' "$CLIP_JSON" | sed -nE 's/.*"token": "([^"]*)".*/\1/p')"
  if [ -z "$CLIP_LABEL" ] || [ -z "$CLIP_TOKEN" ]; then
    echo "ERROR: could not parse the --clip window: $CLIP_SPEC" >&2
    exit 1
  fi
  echo "==> Clip window: $CLIP_LABEL (output timestamps are relative to it)"
fi

# --- Input classification ----------------------------------------------------
# Kaltura inputs are recognised by lib/kaltura.py rather than by a regex here:
# an input may be a bare embed URL *or* a whole pasted <iframe> tag, and the
# same parser has to agree with the one run_one.sh and transcribe.sh use, or a
# blob accepted here fails two stages later. `parse` makes no network call.
is_kaltura_input() {
  "$PYTHON_BIN" "$SCRIPT_DIR/lib/kaltura.py" parse "$1" >/dev/null 2>&1
}

kaltura_safe_name() {
  "$PYTHON_BIN" "$SCRIPT_DIR/lib/kaltura.py" parse "$1" 2>/dev/null \
    | sed -nE 's/.*"safe_name": "([^"]*)".*/\1/p'
}

classify_input() {
  local value="$1"
  if [ -f "$value" ]; then
    echo "local_file"
  elif echo "$value" | grep -qE '(meet\.google\.com/|^https?://[^/]*zoom\.us/)'; then
    echo "meeting"
  elif echo "$value" | grep -qE '(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/playlist\?list=)'; then
    echo "youtube"
  elif is_kaltura_input "$value"; then
    echo "kaltura"
  else
    echo "unknown"
  fi
}

# "Is this argument meant to be an input at all?" — used to keep the legacy
# positional form (<input> <name> <display> <lang> <prompt>) working alongside
# the new multi-input form. A URL or an existing path is an input; a bare word
# like "Weekly Standup" is not.
#
# A pasted Kaltura <iframe> is neither a URL nor a path, so it needs its own
# arm here — without it the blob would be filed as a legacy name positional and
# the run would be created with no input at all.
looks_like_input() {
  case "$1" in
    http://*|https://*) return 0 ;;
    *"<iframe"*) return 0 ;;
  esac
  [ -f "$1" ]
}

# Per-input clip window: "<input>#t=00:05:00-01:30:00".
#
# --clip is one window for the whole invocation, which is the wrong shape when
# several lectures are being summarized together and only some of them want
# trimming — the combined document is per-invocation, so splitting into one
# invocation per window would split the document too. This suffix overrides
# --clip for one input; --clip remains the default for the rest.
#
# Sets SPLIT_INPUT / SPLIT_CLIP_LABEL / SPLIT_CLIP_TOKEN. The suffix is only
# taken as a window when what is LEFT of it still looks like an input, so a URL
# that genuinely ends in some other "#t=" fragment is left alone rather than
# being silently truncated. Once it is taken as a window it must parse, and a
# window that doesn't is fatal here — before classification, before any
# download, before anything is billed.
SPLIT_INPUT=""
SPLIT_CLIP_LABEL=""
SPLIT_CLIP_TOKEN=""
split_clip_suffix() {
  SPLIT_INPUT="$1"
  SPLIT_CLIP_LABEL="$CLIP_LABEL"
  SPLIT_CLIP_TOKEN="$CLIP_TOKEN"
  case "$1" in
    *"#t="*) ;;
    *) return 0 ;;
  esac
  # Both expansions cut at the LAST "#t=", so they agree with each other.
  local spec="${1##*#t=}"
  local rest="${1%#t=*}"
  [ -n "$spec" ] || return 0
  looks_like_input "$rest" || return 0

  local json
  if ! json="$("$PYTHON_BIN" "$SCRIPT_DIR/lib/clip.py" parse "$spec" 2>&1)"; then
    echo "ERROR: unusable #t= window on this input: #t=$spec" >&2
    echo "  ${json#clip: }" >&2
    echo "  Expected #t=START-END, e.g. #t=00:05:00-01:30:00" >&2
    exit 1
  fi
  SPLIT_INPUT="$rest"
  SPLIT_CLIP_LABEL="$(printf '%s' "$json" | sed -nE 's/.*"label": "([^"]*)".*/\1/p')"
  SPLIT_CLIP_TOKEN="$(printf '%s' "$json" | sed -nE 's/.*"token": "([^"]*)".*/\1/p')"
}

declare -a INPUTS=()
# Parallel to INPUTS, one entry each, always — an empty string for an input
# with no window. They are read by index further down, so a push to one without
# a push to the other would silently attach the wrong window to the wrong
# lecture.
declare -a INPUT_CLIP_LABELS=()
declare -a INPUT_CLIP_TOKENS=()
add_input() {
  split_clip_suffix "$1"
  INPUTS+=("$SPLIT_INPUT")
  INPUT_CLIP_LABELS+=("$SPLIT_CLIP_LABEL")
  INPUT_CLIP_TOKENS+=("$SPLIT_CLIP_TOKEN")
}

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
    else
      # Split BEFORE the test, not after: "https://…#t=5:00-10:00" already
      # looks like an input with the suffix still attached, so testing first
      # would take the whole string as the URL and the window would vanish
      # without a word. split_clip_suffix is a no-op on an input that has no
      # window, so this is the same decision as before for everything else.
      split_clip_suffix "$arg"
      if looks_like_input "$SPLIT_INPUT"; then
        INPUTS+=("$SPLIT_INPUT")
        INPUT_CLIP_LABELS+=("$SPLIT_CLIP_LABEL")
        INPUT_CLIP_TOKENS+=("$SPLIT_CLIP_TOKEN")
      else
        LEGACY_EXTRAS+=("$arg")
      fi
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
    # A comment is a "#" at the start of the line or one preceded by
    # whitespace. NOT any "#" at all: that ate the "#t=" window suffix — and
    # every URL fragment before it — leaving a link that still worked and a
    # window that had silently gone.
    line="$(echo "$line" | tr -d '\r' \
            | sed 's/^[[:space:]]*#.*$//; s/[[:space:]]\+#.*$//' \
            | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    add_input "$line"
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
  # Rebuilt alongside EXPANDED. A playlist's window applies to each video it
  # expands into — the alternative is dropping it, and a window the operator
  # typed must never be quietly discarded.
  declare -a EXPANDED_LABELS=()
  declare -a EXPANDED_TOKENS=()
  for idx in "${!INPUTS[@]}"; do
    input="${INPUTS[$idx]}"
    if echo "$input" | grep -qE '[?&]list='; then
      echo "==> Expanding playlist: $input"
      if ! command -v yt-dlp >/dev/null 2>&1; then
        echo "ERROR: --playlist needs yt-dlp. Run ./setup.sh first." >&2
        exit 1
      fi
      while IFS= read -r vid; do
        [ -n "$vid" ] || continue
        EXPANDED+=("https://www.youtube.com/watch?v=$vid")
        EXPANDED_LABELS+=("${INPUT_CLIP_LABELS[$idx]}")
        EXPANDED_TOKENS+=("${INPUT_CLIP_TOKENS[$idx]}")
      done < <(yt-dlp --flat-playlist --print id "$input" 2>/dev/null)
    else
      EXPANDED+=("$input")
      EXPANDED_LABELS+=("${INPUT_CLIP_LABELS[$idx]}")
      EXPANDED_TOKENS+=("${INPUT_CLIP_TOKENS[$idx]}")
    fi
  done
  INPUTS=("${EXPANDED[@]:-}")
  INPUT_CLIP_LABELS=("${EXPANDED_LABELS[@]:-}")
  INPUT_CLIP_TOKENS=("${EXPANDED_TOKENS[@]:-}")
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
    kaltura)
      # kal_<entry id>. The entry id is the only stable handle on a Kaltura
      # lecture — the title is often a date string shared by every session of
      # the course, and would collide.
      local kal
      kal="$(kaltura_safe_name "$input")"
      [ -z "$kal" ] && kal="kaltura"
      echo "$kal"
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
  PAUSED_SKIPPED=0
  for run_dir in "$RUNS_DIR"/*/; do
    [ -f "${run_dir}state.json" ] || continue
    status="$(rs status --run-dir "${run_dir%/}" --stage summarize)"
    [ "$status" = "done" ] && continue
    # A run that ran out of Claude usage window records when the window
    # resets. Until then there is nothing to resume into but the same wall —
    # and this loop runs from a timer, so it must not spend a call finding
    # that out every fifteen minutes.
    resets_at="$(rs get --run-dir "${run_dir%/}" --key stages.summarize.rate_limited.resets_at 2>/dev/null || true)"
    if [ -n "$resets_at" ] && [ "$resets_at" -gt "$(date +%s)" ] 2>/dev/null; then
      echo "==> $(basename "${run_dir%/}"): paused until the Claude usage window resets ($(date -d "@$resets_at" '+%Y-%m-%d %H:%M' 2>/dev/null || echo "$resets_at"))"
      PAUSED_SKIPPED=$((PAUSED_SKIPPED + 1))
      continue
    fi
    # Another pipeline is already working on it (the timer firing while a
    # terminal run is mid-summary, say). run_one.sh would refuse the lock
    # anyway; skipping here keeps that out of the report.
    owner="$(cat "${run_dir}run.lock/pid" 2>/dev/null || true)"
    if [ -n "$owner" ] && kill -0 "$owner" 2>/dev/null; then
      echo "==> $(basename "${run_dir%/}"): in progress under PID $owner — left alone"
      continue
    fi
    # A member of a --combine set never summarizes on its own: its combine
    # run does, and that run is picked up by this same loop. Resuming the
    # member here would bill an individual summary nobody asked for.
    combined_into="$(rs get --run-dir "${run_dir%/}" --key combined_into 2>/dev/null || true)"
    if [ -n "$combined_into" ]; then
      echo "==> $(basename "${run_dir%/}"): part of combine run $combined_into — resumed through it"
      continue
    fi
    RUN_DIRS+=("${run_dir%/}")
  done
  if [ "${#RUN_DIRS[@]}" -eq 0 ]; then
    if [ "$PAUSED_SKIPPED" -gt 0 ]; then
      echo "==> Nothing to resume yet: $PAUSED_SKIPPED run(s) are waiting for the Claude usage window."
    else
      echo "==> Nothing to resume: every run has finished summarizing."
    fi
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
      echo "  Expected a Google Meet or Zoom URL, a YouTube URL, a Kaltura" >&2
      echo "  embed (the <iframe> tag or just its src URL), or a path to a" >&2
      echo "  local media file that exists on disk." >&2
      echo "  (A local path is only recognized if the file is actually there —" >&2
      echo "   check for a typo in the path.)" >&2
      exit 1
    fi
    usage
    exit 1
  fi
  for input_idx in "${!INPUTS[@]}"; do
    input="${INPUTS[$input_idx]}"
    # This input's own window: its #t= suffix if it had one, otherwise the
    # invocation-wide --clip. Read by index rather than carried in $input,
    # because the input string is also the auto-resume key and the provenance
    # link — a window smuggled inside it would end up in both.
    this_clip_label="${INPUT_CLIP_LABELS[$input_idx]:-}"
    this_clip_token="${INPUT_CLIP_TOKENS[$input_idx]:-}"
    kind="$(classify_input "$input")"
    if [ "$kind" = "meeting" ] && [ -n "$this_clip_label" ]; then
      echo "ERROR: a clip window does not apply to a live meeting: $input" >&2
      echo "  The window is cut out of an existing recording, and this input" >&2
      echo "  has none yet. Record it first, then clip the MP4:" >&2
      echo "    ./pipeline.sh \"\$RECORDINGS_DIR/<run_id>.mp4\" --clip $this_clip_label" >&2
      exit 1
    fi
    if [ "$kind" = "unknown" ]; then
      echo "ERROR: unrecognized input: $input" >&2
      echo "  Expected a Google Meet or Zoom URL, a YouTube URL, a Kaltura" >&2
      echo "  embed (the <iframe> tag or just its src URL), or a path to a" >&2
      echo "  local media file that exists." >&2
      exit 1
    fi

    # Auto-resume: an unfinished run for this exact input gets picked up rather
    # than duplicated. --force always starts a clean run instead.
    existing=""
    if [ "$FORCE" -eq 0 ]; then
      existing="$(rs find --root "$RUNS_DIR" --input "$input" --clip "$this_clip_label" --incomplete 2>/dev/null || true)"
    fi

    if [ -n "$existing" ]; then
      echo "==> Resuming unfinished run for $input"
      echo "    run id: $existing   (use --force to start over instead)"
      run_dir="$RUNS_DIR/$existing"
    else
      safe="$(derive_safe_name "$input" "$kind")"
      [ -z "$safe" ] && safe="meeting"
      [ -n "$this_clip_token" ] && safe="${safe}_${this_clip_token}"
      run_dir="$RUNS_DIR/${safe}_$(date +%Y%m%d_%H%M%S)"
      # Two inputs starting in the same second would otherwise share a run dir.
      suffix=1
      while [ -d "$run_dir" ]; do
        run_dir="$RUNS_DIR/${safe}_$(date +%Y%m%d_%H%M%S)_$suffix"
        suffix=$((suffix + 1))
      done
      declare -a init_args=(
        --run-dir "$run_dir"
        --input "$input" --input-type "$kind"
        --name "${NAME:-$safe}" --safe-name "$safe"
        --language "$LANGUAGE" --prompt "$PROMPT_NAME"
        --display-name "$DISPLAY_NAME"
      )
      [ -n "$this_clip_label" ] && init_args+=(--clip "$this_clip_label")
      [ -n "$this_clip_label" ] \
        && echo "==> $input" && echo "    clip: $this_clip_label"
      for spec in "${RESOURCE_SPECS[@]:-}"; do
        [ -n "$spec" ] && init_args+=(--resources "$spec")
      done
      rs init "${init_args[@]}"
    fi
    RUN_DIRS+=("$run_dir")
  done
fi

# --- The combine run: settled before any member starts ----------------------
# --combine makes one more run, whose only stage is a summarize over every
# member's transcript and frames (see run_one.sh, "A combine run"). It is
# resolved here, before the members launch, for two reasons: the members have
# to be told they are members (so their own summarize stage is skipped, and
# --resume-all leaves them alone), and the auto-resume key — the member run
# ids, in order — is only known once they are.
COMBINE_RUN_DIR=""
if [ -n "$COMBINE_FILE" ]; then
  if [ -n "$EXPLICIT_RUN_ID" ] || [ "$RESUME_LAST" -eq 1 ] || [ "$RESUME_ALL" -eq 1 ]; then
    echo "ERROR: --combine takes inputs, not --run-id/--resume-last/--resume-all." >&2
    echo "  To resume a combined summary, re-run the original command, or" >&2
    echo "  ./pipeline.sh --run-id <combine_...>  (see --list)." >&2
    exit 1
  fi
  case "$COMBINE_FILE" in
    /*) ;;
    *) COMBINE_FILE="$PWD/$COMBINE_FILE" ;;
  esac
  if [ "$COMBINE_WANT_PDF" -eq 1 ]; then
    case "$(printf '%s' "${SUMMARY_WRITE_PDF:-1}" | tr 'A-Z' 'a-z')" in
      0|false|no) COMBINE_PDF="" ;;
      *)
        [ -n "$COMBINE_PDF" ] || COMBINE_PDF="${COMBINE_FILE%.md}.pdf"
        case "$COMBINE_PDF" in
          /*) ;;
          *) COMBINE_PDF="$PWD/$COMBINE_PDF" ;;
        esac
        ;;
    esac
  else
    COMBINE_PDF=""
  fi

  declare -a MEMBER_IDS=()
  for run_dir in "${RUN_DIRS[@]}"; do
    MEMBER_IDS+=("$(basename "$run_dir")")
  done
  combine_key="$("$PYTHON_BIN" "$SCRIPT_DIR/lib/combine.py" run-key "${MEMBER_IDS[@]}")"
  # Not --incomplete, unlike a member's auto-resume. The members are always
  # "incomplete" (their summarize never runs), so the same command always
  # resumes the same members and lands on the same key — and a combine run
  # that already finished must then be reported as done, not re-summarized.
  # --force starts the members over, which makes a new key anyway.
  existing=""
  if [ "$FORCE" -eq 0 ]; then
    existing="$(rs find --root "$RUNS_DIR" --input "$combine_key" 2>/dev/null || true)"
  fi
  if [ -n "$existing" ]; then
    echo "==> Resuming combined summary: $existing"
    COMBINE_RUN_DIR="$RUNS_DIR/$existing"
  else
    combine_safe="$("$PYTHON_BIN" "$SCRIPT_DIR/lib/combine.py" safe-name "${MEMBER_IDS[@]}")"
    COMBINE_RUN_DIR="$RUNS_DIR/${combine_safe}_$(date +%Y%m%d_%H%M%S)"
    suffix=1
    while [ -d "$COMBINE_RUN_DIR" ]; do
      COMBINE_RUN_DIR="$RUNS_DIR/${combine_safe}_$(date +%Y%m%d_%H%M%S)_$suffix"
      suffix=$((suffix + 1))
    done
  fi
  # `init` refreshes the metadata on a resume too, so a re-run that names a
  # different --combine path writes there.
  declare -a combine_init=(
    --run-dir "$COMBINE_RUN_DIR"
    --input "$combine_key" --input-type combine
    --name "$(basename "${COMBINE_FILE%.md}")" --safe-name "$(basename "$COMBINE_RUN_DIR")"
    --language "$LANGUAGE" --prompt "$PROMPT_NAME"
    --display-name "$DISPLAY_NAME"
    --output-md "$COMBINE_FILE"
  )
  [ -n "$COMBINE_PDF" ] && combine_init+=(--output-pdf "$COMBINE_PDF")
  for member in "${MEMBER_IDS[@]}"; do
    combine_init+=(--members "$member")
  done
  for spec in "${RESOURCE_SPECS[@]:-}"; do
    [ -n "$spec" ] && combine_init+=(--resources "$spec")
  done
  mkdir -p "$RUNS_DIR"
  rs init "${combine_init[@]}"
  # And the reverse pointer on every member.
  for run_dir in "${RUN_DIRS[@]}"; do
    rs init --run-dir "$run_dir" --combined-into "$(basename "$COMBINE_RUN_DIR")"
  done
  echo "==> Combined summary: $COMBINE_FILE"
  [ -n "$COMBINE_PDF" ] && echo "    PDF:              $COMBINE_PDF"
  echo "    run id:           $(basename "$COMBINE_RUN_DIR")"
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
  # Members of a --combine set stop after transcribe and frames; the combine
  # run below is what summarizes them.
  [ -n "$COMBINE_RUN_DIR" ] && args+=(--skip-summarize)

  (
    if [ "$TOTAL" -gt 1 ]; then
      # Prefix every line so concurrent runs stay readable. awk with an
      # explicit fflush() rather than `sed -u`, which is a GNU extension.
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
PAUSED=0
for run_dir in "${RUN_DIRS[@]}"; do
  run_id="$(basename "$run_dir")"
  rc="$(cat "$RESULT_DIR/$run_id" 2>/dev/null || echo "?")"
  if [ "$rc" = "$EXIT_PAUSED" ]; then
    # Out of Claude usage window, not broken. The reset time is in
    # state.json; --resume-all (by hand or from the timer) picks it up.
    PAUSED=$((PAUSED + 1))
    resumes_at="$(rs get --run-dir "$run_dir" --key stages.summarize.rate_limited.resets_at_iso 2>/dev/null || true)"
    echo "  PAUSED $run_id  (Claude usage window; resumable after ${resumes_at:-the reset})"
    echo "        resume:   ./pipeline.sh --resume-all"
  elif [ "$rc" = "0" ]; then
    echo "  OK    $run_id"
    if [ -n "$COMBINE_RUN_DIR" ]; then
      txt="$(rs get --run-dir "$run_dir" --key stages.transcribe.artifacts.txt 2>/dev/null || true)"
      [ -n "$txt" ] && echo "        -> $txt"
    else
      summary="$(rs get --run-dir "$run_dir" --key stages.summarize.artifacts.md 2>/dev/null || true)"
      pdf="$(rs get --run-dir "$run_dir" --key stages.summarize.artifacts.pdf 2>/dev/null || true)"
      [ -n "$summary" ] && echo "        -> $summary"
      [ -n "$pdf" ] && echo "        -> $pdf"
    fi
  else
    FAILED=$((FAILED + 1))
    echo "  FAIL  $run_id  (exit $rc)"
    echo "        details:  ./pipeline.sh --status $run_id"
    echo "        resume:   ./pipeline.sh --run-id $run_id"
  fi
done

# --- The combined summary ----------------------------------------------------
# Only once every member has its transcript and frames. A member that failed
# leaves the combine run untouched — nothing has been spent on it yet — and
# the same command resumes both.
COMBINE_RC=0
if [ -n "$COMBINE_RUN_DIR" ]; then
  echo ""
  if [ "$FAILED" -gt 0 ]; then
    echo "==> Combined summary not attempted: $FAILED member run(s) failed."
    echo "    Fix or resume them, then re-run this same command — the members"
    echo "    that finished are kept, and the combined summary is made once"
    echo "    all of them are ready."
    COMBINE_RC=1
  else
    combine_id="$(basename "$COMBINE_RUN_DIR")"
    declare -a combine_args=(--run-dir "$COMBINE_RUN_DIR")
    [ "$FORCE" -eq 1 ] && combine_args+=(--force)
    combine_run_rc=0
    bash "$SCRIPT_DIR/lib/run_one.sh" "${combine_args[@]}" || combine_run_rc=$?
    if [ "$combine_run_rc" -eq 0 ]; then
      echo ""
      echo "  OK    $combine_id  (combined summary)"
      md="$(rs get --run-dir "$COMBINE_RUN_DIR" --key stages.summarize.artifacts.md 2>/dev/null || true)"
      pdf="$(rs get --run-dir "$COMBINE_RUN_DIR" --key stages.summarize.artifacts.pdf 2>/dev/null || true)"
      [ -n "$md" ] && echo "        -> $md"
      [ -n "$pdf" ] && echo "        -> $pdf"
    elif [ "$combine_run_rc" -eq "$EXIT_PAUSED" ]; then
      PAUSED=$((PAUSED + 1))
      resumes_at="$(rs get --run-dir "$COMBINE_RUN_DIR" --key stages.summarize.rate_limited.resets_at_iso 2>/dev/null || true)"
      echo ""
      echo "  PAUSED $combine_id  (combined summary; Claude usage window, resumable after ${resumes_at:-the reset})"
      echo "        resume:   ./pipeline.sh --resume-all"
    else
      COMBINE_RC=1
      echo ""
      echo "  FAIL  $combine_id  (combined summary)"
      echo "        details:  ./pipeline.sh --status $combine_id"
      echo "        resume:   ./pipeline.sh --run-id $combine_id"
    fi
  fi
fi

if [ "$FAILED" -gt 0 ]; then
  echo ""
  echo "$FAILED of $TOTAL run(s) failed. Everything that succeeded is saved —"
  echo "resuming re-runs only the stages that didn't finish."
  exit 1
fi
[ "$COMBINE_RC" -eq 0 ] || exit 1
if [ "$PAUSED" -gt 0 ]; then
  echo ""
  echo "$PAUSED run(s) are waiting for the Claude usage window to reset. Nothing"
  echo "is lost: transcripts and frames are kept, and ./pipeline.sh --resume-all"
  echo "(or the meeting-bot-resume timer, if installed) finishes them."
  exit "$EXIT_PAUSED"
fi
