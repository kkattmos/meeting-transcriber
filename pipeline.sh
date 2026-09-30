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
#   ./pipeline.sh --new-meet --name "Project sync"   (the bot CREATES a Meet)
#   ./pipeline.sh /path/to/recording.mp4 --language en
#   ./pipeline.sh "https://youtu.be/bbb" --clip 00:05:00-01:30:00
#
# RESUMING. State lives in $MEETING_BOT_ROOT/runs/<run_id>/ (default ~/.local/share/meeting-bot). If a run fails
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
#   ./pipeline.sh <inputs...> --dry-run check everything (inputs, windows,
#                                       resources) and print what would run,
#                                       without creating or starting anything
#
# Options:
#   --name N            meeting name (single input only; otherwise derived)
#   --display-name D    name the bot shows in the meeting (default "Meeting Bot")
#   --language L        th (default), en, auto, or any AssemblyAI language code
#   --prompt P          the summary style: video, meeting, lecture, tutorial
#                       or reality (a reality-show episode recap, with
#                       timestamps; a file in summarize/prompts/; the older
#                       names such as lecture-claude still work)
#   --summary-language L  the language the summary is WRITTEN in: th or en
#                       (default SUMMARY_LANGUAGE). --language is the SPOKEN one.
#   --pdf-font F        the PDF's body font. Thai: "Bai Jamjuree" or Sarabun;
#                       English: "CMU Serif" (Computer Modern), Sarabun or
#                       "Bai Jamjuree". Default PDF_FONT_TH / PDF_FONT_EN.
#   --instructions T    extra instructions for the summarizer, for this run
#                       ("focus on the exam hints", "skip the admin part").
#                       These three are stored with the run, so a resume uses
#                       them; given again on a resume, they replace the old ones.
#   --summary-source S  what the summary is made from: both (default: the
#                       transcript and the frames) or voice (the transcript
#                       alone — no frames are extracted, and a YouTube video
#                       is not even downloaded). Default SUMMARY_SOURCE.
#                       --voice-only is --summary-source voice. Stored with
#                       the run like the three above.
#   --record-media M    what a meeting's recording keeps: video (default: an
#                       MP4 of the screen and the audio) or audio (an .m4a;
#                       the bot's browser renders at RECORD_AUDIO_GEOMETRY,
#                       960x540, and no frames are possible, so the summary
#                       is from the voice). Default RECORD_MEDIA. Meetings
#                       only; other inputs ignore it. --audio-only is
#                       --record-media audio. Fixed when the run is created.
#   --resources SPEC    slides / notes for this session, as a GitHub repo
#                       (optionally @branch, or a /tree/<branch>/<subdir> URL)
#                       or a local file or folder. Repeatable. Their text is
#                       given to the summarizer as reference material and their
#                       slide images are embedded in the PDF. Every --resources
#                       applies to every run in the invocation. A Markdown file
#                       may start with frontmatter (course, source,
#                       citation_label, coverage) — the lecture prompt then
#                       cites it by that label.
#                       A binary file named .md/.txt is refused up front.
#   --source-url URL    the link the summary cites for a LOCAL FILE input, in
#                       place of the file's path: the call it was recorded
#                       from (the Discord bot passes the voice channel's
#                       https://discord.com/channels/... link). One local-file
#                       input only; stored with the run.
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
#   --new-meet          create a new Google Meet (meet.new) in the bot's signed-in
#                       browser profile (BOT_GOOGLE_ACCOUNT), host it, and record it. The link is
#                       printed as soon as it exists and kept in the run
#                       (./pipeline.sh --status <run_id>). The bot admits
#                       everyone who asks to join, waits NEW_MEET_WAIT_MINUTES
#                       (15) for the first one, and ends the call for everyone
#                       once they have all left. "meet.new" as an input is the
#                       same thing. Never auto-resumed: each one is a new call.
#   --foreground        stay attached to this terminal. By default an invocation
#                       that records a meeting (a Meet/Zoom link, --new-meet)
#                       detaches: the whole run continues in the background —
#                       closing the terminal does not stop it — and this
#                       command returns at once with the run id, the log to
#                       follow and how to stop it. Everything else (YouTube,
#                       Kaltura, files) runs in the foreground as before.
#                       MEETING_BOT_FOREGROUND=1 in the environment is the same.
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
# Kept verbatim for the background re-launch below.
declare -a ORIGINAL_ARGS=("$@")
FOREGROUND="${MEETING_BOT_FOREGROUND:-0}"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib/paths.sh"

RUNS_DIR="$MEETING_BOT_ROOT/runs"
RUNSTATE="$SCRIPT_DIR/lib/runstate.py"

PYTHON_BIN="${MEETING_BOT_VENV:-$LOADER_DIR/.venv}/bin/python3"
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
NEW_MEET=0
DRY_RUN=0
# --dry-run reports every input it can't use (a `bad`, `badarg` or `extra` line each)
# instead of stopping at the first, so the web UI can mark each line; this
# counts them, and the dry run exits 1 at the end if there were any.
DRY_BAD=0
SUMMARY_LANG_OPT=""
PDF_FONT_OPT=""
INSTRUCTIONS_OPT=""
SUMMARY_SOURCE_OPT=""
RECORD_MEDIA_OPT=""
SOURCE_URL_OPT=""
declare -a POSITIONAL=()
declare -a RESOURCE_SPECS=()
# RESOURCES in .env is the default for every run; --resources adds to it.
if [ -n "${RESOURCES:-}" ]; then
  while IFS= read -r _spec; do
    _spec="$(printf '%s' "$_spec" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')"
    [ -n "$_spec" ] && RESOURCE_SPECS+=("$_spec")
  done < <(printf '%s\n' "$RESOURCES" | tr ',' '\n')
fi

# Everything from line 2 up to `set -uo pipefail`: a fixed line range went stale
# every time an option was documented and silently cut the help short.
usage() { awk 'NR > 1 && /^set -uo pipefail/ { exit } NR > 1' "$0"; }

# Every value-taking option goes through need_value. Without it, an option left
# last on the line ("... --combine") made `shift 2` fail with one argument left
# — which under `set -u` without `-e` changes nothing, so this loop spun on the
# same argument forever, printing nothing and burning a core. A value that is
# itself an option ("--combine --jobs 1") is the same typo one step earlier.
need_value() {
  if [ "$2" -lt 2 ] || [ -z "$3" ] || [ "${3#--}" != "$3" ]; then
    echo "ERROR: $1 needs a value (try --help)" >&2
    exit 1
  fi
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --name)         need_value "$1" "$#" "${2:-}"; NAME="$2"; shift 2 ;;
    --display-name) need_value "$1" "$#" "${2:-}"; DISPLAY_NAME="$2"; shift 2 ;;
    --language)     need_value "$1" "$#" "${2:-}"; LANGUAGE="$2"; shift 2 ;;
    --prompt)       need_value "$1" "$#" "${2:-}"; PROMPT_NAME="$2"; shift 2 ;;
    --summary-language) need_value "$1" "$#" "${2:-}"; SUMMARY_LANG_OPT="$2"; shift 2 ;;
    --pdf-font)     need_value "$1" "$#" "${2:-}"; PDF_FONT_OPT="$2"; shift 2 ;;
    --instructions) need_value "$1" "$#" "${2:-}"; INSTRUCTIONS_OPT="$2"; shift 2 ;;
    --summary-source) need_value "$1" "$#" "${2:-}"; SUMMARY_SOURCE_OPT="$2"; shift 2 ;;
    --record-media) need_value "$1" "$#" "${2:-}"; RECORD_MEDIA_OPT="$2"; shift 2 ;;
    --source-url)   need_value "$1" "$#" "${2:-}"; SOURCE_URL_OPT="$2"; shift 2 ;;
    --voice-only)   SUMMARY_SOURCE_OPT=voice; shift ;;
    --audio-only)   RECORD_MEDIA_OPT=audio; shift ;;
    --jobs)         need_value "$1" "$#" "${2:-}"; JOBS="$2"; shift 2 ;;
    --clip)         need_value "$1" "$#" "${2:-}"; CLIP_SPEC="$2"; shift 2 ;;
    --from-file)    need_value "$1" "$#" "${2:-}"; FROM_FILE="$2"; shift 2 ;;
    --combine)      need_value "$1" "$#" "${2:-}"; COMBINE_FILE="$2"; shift 2 ;;
    --combine-pdf)  need_value "$1" "$#" "${2:-}"; COMBINE_PDF="$2"; shift 2 ;;
    --no-combine-pdf) COMBINE_WANT_PDF=0; shift ;;
    --run-id)       need_value "$1" "$#" "${2:-}"; EXPLICIT_RUN_ID="$2"; shift 2 ;;
    --resources)    need_value "$1" "$#" "${2:-}"; RESOURCE_SPECS+=("$2"); shift 2 ;;
    --new-meet)     NEW_MEET=1; shift ;;
    --dry-run)      DRY_RUN=1; shift ;;
    --foreground)   FOREGROUND=1; shift ;;
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

case "$JOBS" in
  ''|*[!0-9]*|0) echo "ERROR: --jobs needs a positive whole number, got: $JOBS" >&2; exit 1 ;;
esac

# --- The summary's language and font, settled before anything is paid for ----
# Both are checked by the same modules summarize.py uses, so a value accepted
# here is one the summary stage will honour. The font is checked against the
# language the summary will actually be written in: Computer Modern has no
# Thai, so "CMU Serif" is refused for a Thai summary here rather than being
# swapped for the default two hours later.
declare -a SUMMARY_INIT_ARGS=()
if [ -n "$SUMMARY_LANG_OPT" ]; then
  SUMMARY_LANG_OPT="$(cd "$SCRIPT_DIR/summarize" && "$PYTHON_BIN" -c \
    'import sys, language
try: print(language.normalize(sys.argv[1]))
except language.UnknownLanguage as e: sys.exit(f"ERROR: --summary-language: {e}")' \
    "$SUMMARY_LANG_OPT")" || exit 1
  SUMMARY_INIT_ARGS+=(--summary-language "$SUMMARY_LANG_OPT")
fi
if [ -n "$PDF_FONT_OPT" ]; then
  PDF_FONT_OPT="$("$PYTHON_BIN" "$SCRIPT_DIR/summarize/fontchoice.py" check \
    --language "${SUMMARY_LANG_OPT:-${SUMMARY_LANGUAGE:-th}}" --font "$PDF_FONT_OPT")" \
    || { echo "  (from --pdf-font)" >&2; exit 1; }
  SUMMARY_INIT_ARGS+=(--pdf-font "$PDF_FONT_OPT")
fi
[ -n "$INSTRUCTIONS_OPT" ] && SUMMARY_INIT_ARGS+=(--instructions "$INSTRUCTIONS_OPT")

# --- What the summary is made from, and what a meeting records -----------------
# Flag first, then .env (SUMMARY_SOURCE / RECORD_MEDIA), then today's
# behaviour. A typo in either is refused here, before anything is paid for.
media_choice() {  # <what> <value> <allowed...>
  local what="$1" value="$2" ok
  shift 2
  for ok in "$@"; do [ "$value" = "$ok" ] && return 0; done
  echo "ERROR: $what must be one of: $*  (got: $value)" >&2
  exit 1
}
SUMMARY_SOURCE_EFF="${SUMMARY_SOURCE_OPT:-${SUMMARY_SOURCE:-both}}"
RECORD_MEDIA_EFF="${RECORD_MEDIA_OPT:-${RECORD_MEDIA:-video}}"
media_choice "${SUMMARY_SOURCE_OPT:+--summary-source}${SUMMARY_SOURCE_OPT:-SUMMARY_SOURCE}" \
  "$SUMMARY_SOURCE_EFF" both voice
media_choice "${RECORD_MEDIA_OPT:+--record-media}${RECORD_MEDIA_OPT:-RECORD_MEDIA}" \
  "$RECORD_MEDIA_EFF" video audio
# Replaces the stored value on a resume only when asked for explicitly, like
# the summary settings above. The recording medium never changes on a
# resume: the file is already named for it (and may already exist).
[ -n "$SUMMARY_SOURCE_OPT" ] && SUMMARY_INIT_ARGS+=(--summary-source "$SUMMARY_SOURCE_OPT")

# --- The link a local recording cites ------------------------------------------
# A file recorded elsewhere (the Discord bot's mix) is a path on this disk; the
# document should name the call instead. Checked for shape here; that it goes
# with exactly one local file is checked once the inputs are classified.
if [ -n "$SOURCE_URL_OPT" ]; then
  case "$SOURCE_URL_OPT" in
    https://?*) ;;
    *) echo "ERROR: --source-url must be an https:// link (got: $SOURCE_URL_OPT)" >&2
       exit 1 ;;
  esac
  case "$SOURCE_URL_OPT" in
    *[[:space:]]*) echo "ERROR: --source-url must not contain spaces" >&2; exit 1 ;;
  esac
  SUMMARY_INIT_ARGS+=(--source-url "$SOURCE_URL_OPT")
fi

# --- Meetings run in the background -------------------------------------------
# A recording lasts as long as the meeting, and it must not die with the
# terminal that started it. So an invocation that records anything re-launches
# itself in its own session (setsid: no controlling terminal, so no SIGHUP when
# the window closes) with output to a log, and returns. Decided before any
# state is written, so the background copy is the only one that creates runs —
# a meet.new run is never auto-resumed, and two copies would make two calls.
# Scanned loosely (a URL anywhere in the arguments or --from-file); the
# background copy does the real classification, and --dry-run never detaches.
invocation_records_a_meeting() {
  [ "$NEW_MEET" -eq 1 ] && return 0
  local text
  text="$(printf '%s\n' "${POSITIONAL[@]}")"
  if [ -n "$FROM_FILE" ] && [ -f "$FROM_FILE" ]; then
    text+=$'\n'"$(cat "$FROM_FILE")"
  fi
  printf '%s\n' "$text" | grep -qiE '(meet\.google\.com/|zoom\.us/|^(https?://)?meet\.new/?$|^new-meet$)'
}
if [ "$FOREGROUND" != "1" ] && [ "$DRY_RUN" -eq 0 ] && [ "$RESUME_ALL" -eq 0 ] \
   && [ -z "$EXPLICIT_RUN_ID" ] && [ "${RESUME_LAST:-0}" -eq 0 ] \
   && invocation_records_a_meeting; then
  BG_LOG_DIR="$MEETING_BOT_ROOT/logs"
  mkdir -p "$BG_LOG_DIR"
  BG_LOG="$BG_LOG_DIR/pipeline_$(date +%Y%m%d_%H%M%S)_$$.log"
  MEETING_BOT_FOREGROUND=1 setsid nohup bash "$SCRIPT_DIR/pipeline.sh" "${ORIGINAL_ARGS[@]}" \
    > "$BG_LOG" 2>&1 < /dev/null &
  BG_PID=$!
  echo "==> Recording in the background (pid $BG_PID). Closing this terminal won't stop it."
  echo "    Log:    tail -f '$BG_LOG'"
  # The run id (and a created meeting's link) appear in the log within a few
  # seconds; show them if they do, so the operator can stop the right run.
  for _ in $(seq 1 20); do
    grep -qE '^Run: |New Google Meet:' "$BG_LOG" 2>/dev/null && break
    kill -0 "$BG_PID" 2>/dev/null || break
    sleep 0.5
  done
  grep -E '^Run: ' "$BG_LOG" 2>/dev/null | sed 's/^Run: /    Run:    /' | sort -u
  grep -E 'New Google Meet:' "$BG_LOG" 2>/dev/null | sed 's/^ */    /'
  if ! kill -0 "$BG_PID" 2>/dev/null; then
    echo "==> The background run already exited — see the log:" >&2
    tail -n 20 "$BG_LOG" >&2
    exit 1
  fi
  echo "    Status: ./pipeline.sh --list    (or ./pipeline.sh --status <run id>)"
  echo "    Stop:   ./kill_meeting.sh --run-id <run id>   (leaves the call cleanly)"
  exit 0
fi

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

# --- Reference material, checked before anything is paid for ----------------
# A missing local path, or a "notes.md" that is really a PDF, used to surface
# in the summarize stage — after the recording, the AssemblyAI charge and the
# frame pass. Offline: a GitHub spec is only parsed here, fetched later.
if [ "${#RESOURCE_SPECS[@]}" -gt 0 ]; then
  "$PYTHON_BIN" "$SCRIPT_DIR/lib/resources.py" check "${RESOURCE_SPECS[@]}" || exit 1
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
  elif echo "$value" | grep -qE '(meet\.google\.com/|^https?://meet\.new/?$|^https?://[^/]*zoom\.us/)'; then
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
    meet.new|meet.new/|new-meet) return 0 ;;
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
    if [ "$DRY_RUN" -eq 1 ]; then
      # The whole argument, as typed: that is what the web UI matches on.
      printf 'badarg\tunusable #t= window: %s\t%s\n' "${json#clip: }" "$1"
      DRY_BAD=$((DRY_BAD + 1))
      return 1
    fi
    echo "ERROR: unusable #t= window on this input: #t=$spec" >&2
    echo "  ${json#clip: }" >&2
    echo "  Expected #t=START-END, e.g. #t=00:05:00-01:30:00" >&2
    exit 1
  fi
  SPLIT_INPUT="$rest"
  SPLIT_CLIP_LABEL="$(printf '%s' "$json" | sed -nE 's/.*"label": "([^"]*)".*/\1/p')"
  SPLIT_CLIP_TOKEN="$(printf '%s' "$json" | sed -nE 's/.*"token": "([^"]*)".*/\1/p')"
}

# "Create a meeting" has several spellings; one canonical input string, so the
# stored input, the run's link line and is_new_meet all agree.
NEW_MEET_INPUT="https://meet.new"
canonical_input() {
  case "$1" in
    meet.new|meet.new/|new-meet|http://meet.new|http://meet.new/|https://meet.new/)
      echo "$NEW_MEET_INPUT" ;;
    *) echo "$1" ;;
  esac
}

declare -a INPUTS=()
# Parallel to INPUTS, one entry each, always — an empty string for an input
# with no window. They are read by index further down, so a push to one without
# a push to the other would silently attach the wrong window to the wrong
# lecture.
declare -a INPUT_CLIP_LABELS=()
declare -a INPUT_CLIP_TOKENS=()
add_input() {
  split_clip_suffix "$1" || return 0   # dry run only: already reported
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
      split_clip_suffix "$arg" || continue   # dry run only: already reported
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
  # A PDF read line by line is a list of garbage "inputs" (lost an hour to
  # exactly that). --from-file wants a text file of links and paths; slides
  # and notes belong in --resources.
  if "$PYTHON_BIN" "$SCRIPT_DIR/lib/resources.py" is-binary "$FROM_FILE"; then
    echo "ERROR: --from-file: $FROM_FILE: this looks like a binary document; convert it to Markdown first." >&2
    echo "  --from-file takes a text file of links/paths, one per line." >&2
    echo "  Slides and course notes go in --resources instead." >&2
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

# --new-meet is one more input, after everything else on the line.
[ "$NEW_MEET" -eq 1 ] && add_input "$NEW_MEET_INPUT"

# A dry run names every argument that is not an input. On the command line
# one may be the legacy form's name; the web UI, whose every line is meant as
# an input, marks each of these as not recognised.
if [ "$DRY_RUN" -eq 1 ]; then
  for extra in "${LEGACY_EXTRAS[@]:-}"; do
    [ -n "$extra" ] && printf 'extra\t%s\n' "$extra"
  done
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
    # A dry run has named them (`extra` lines); it checks the rest too.
    if [ "$DRY_RUN" -eq 1 ]; then DRY_BAD=$((DRY_BAD + ${#REAL_EXTRAS[@]})); else exit 1; fi
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
      if [ "$input" = "$NEW_MEET_INPUT" ]; then
        sanitize "${NAME:-new_meet}"
      else
        sanitize "${NAME:-meeting}"
      fi
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
      echo "  Expected a Google Meet or Zoom URL (or meet.new to create one)," >&2
      echo "  a YouTube URL, a Kaltura embed (the <iframe> tag or just its src" >&2
      echo "  URL), or a path to a local media file that exists on disk." >&2
      echo "  (A local path is only recognized if the file is actually there —" >&2
      echo "   check for a typo in the path.)" >&2
      exit 1
    fi
    # Every input was refused at parse time (a bad #t= window), and said so.
    [ "$DRY_BAD" -gt 0 ] && exit 1
    usage
    exit 1
  fi
  # A dry run reports each unusable input and goes on to the next.
  dry_bad() {  # <reason> <input>
    printf 'bad\t%s\t%s\n' "$1" "$2"
    DRY_BAD=$((DRY_BAD + 1))
  }
  for input_idx in "${!INPUTS[@]}"; do
    input="$(canonical_input "${INPUTS[$input_idx]}")"
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
      if [ "$DRY_RUN" -eq 1 ]; then
        dry_bad "a clip window does not apply to a live meeting" "$input"; continue
      fi
      exit 1
    fi
    # An audio recording has no picture to take frames from. Said at second
    # zero rather than discovered as a voice-only summary the operator did
    # not ask for.
    if [ "$kind" = "meeting" ] && [ "$RECORD_MEDIA_EFF" = "audio" ] \
       && [ "$SUMMARY_SOURCE_OPT" = "both" ]; then
      echo "ERROR: --summary-source both needs a video recording, but this meeting" >&2
      echo "  records audio only (${RECORD_MEDIA_OPT:+--record-media audio}${RECORD_MEDIA_OPT:-RECORD_MEDIA=audio in .env})." >&2
      echo "  Use --record-media video, or summarize from the voice." >&2
      if [ "$DRY_RUN" -eq 1 ]; then
        dry_bad "an audio-only recording can only be summarized from the voice" "$input"; continue
      fi
      exit 1
    fi
    if [ "$kind" = "unknown" ]; then
      echo "ERROR: unrecognized input: $input" >&2
      echo "  Expected a Google Meet or Zoom URL (or meet.new to create one)," >&2
      echo "  a YouTube URL, a Kaltura embed (the <iframe> tag or just its src" >&2
      echo "  URL), or a path to a local media file that exists." >&2
      if [ "$DRY_RUN" -eq 1 ]; then
        dry_bad "not recognised: expected a Meet/Zoom link, a YouTube link, a Kaltura embed, or a file that exists on this machine" "$input"
        continue
      fi
      exit 1
    fi
    if [ -n "$SOURCE_URL_OPT" ] && { [ "$kind" != "local_file" ] \
         || [ "${#INPUTS[@]}" -ne 1 ] || [ -n "$COMBINE_FILE" ]; }; then
      echo "ERROR: --source-url names the call ONE local recording came from;" >&2
      echo "  it cannot go with a $kind input, several inputs, or --combine." >&2
      if [ "$DRY_RUN" -eq 1 ]; then
        dry_bad "--source-url applies to a single local file only" "$input"; continue
      fi
      exit 1
    fi

    # Auto-resume: an unfinished run for this exact input gets picked up rather
    # than duplicated. --force always starts a clean run instead.
    # Not for a meeting the bot creates: every meet.new is a different call,
    # and "resuming" an older one would transcribe last week's meeting in
    # place of recording this one. Its link is in that run's state; resume it
    # explicitly with --run-id.
    existing=""
    if [ "$FORCE" -eq 0 ] && [ "$input" != "$NEW_MEET_INPUT" ]; then
      existing="$(rs find --root "$RUNS_DIR" --input "$input" --clip "$this_clip_label" --incomplete 2>/dev/null || true)"
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
      # One tab-separated line per input: what the web UI's "Check" shows.
      printf 'ok\t%s\t%s\t%s\t%s\n' "$kind" "${this_clip_label:--}" \
        "${existing:-new}" "$input"
      continue
    fi
    if [ -n "$existing" ]; then
      echo "==> Resuming unfinished run for $input"
      echo "    run id: $existing   (use --force to start over instead)"
      run_dir="$RUNS_DIR/$existing"
      # Its summary has not been written yet (that is what "unfinished"
      # means), so summary settings given now are the ones it should use.
      if [ "${#SUMMARY_INIT_ARGS[@]}" -gt 0 ]; then
        rs init --run-dir "$run_dir" "${SUMMARY_INIT_ARGS[@]}"
      fi
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
        "${SUMMARY_INIT_ARGS[@]}"
        --summary-source "$SUMMARY_SOURCE_EFF"
      )
      if [ "$kind" = "meeting" ]; then
        init_args+=(--record-media "$RECORD_MEDIA_EFF")
        # No picture, no frames: stored as voice so --status says what the
        # summary is made from.
        [ "$RECORD_MEDIA_EFF" = "audio" ] && init_args+=(--summary-source voice)
      fi
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

if [ "$DRY_RUN" -eq 1 ]; then
  if [ -n "$COMBINE_FILE" ]; then
    combine_dir="$(dirname "$COMBINE_FILE")"
    case "$combine_dir" in /*) ;; *) combine_dir="$PWD/$combine_dir" ;; esac
    if [ ! -d "$combine_dir" ]; then
      echo "ERROR: --combine: the directory does not exist: $combine_dir" >&2
      exit 1
    fi
    printf 'combine\t%s\n' "$COMBINE_FILE"
  fi
  for run_dir in "${RUN_DIRS[@]:-}"; do
    [ -n "$run_dir" ] && printf 'resume\t%s\n' "$(basename "$run_dir")"
  done
  # On stderr, which the web UI's Check shows under "Looks good".
  _lang="${SUMMARY_LANG_OPT:-${SUMMARY_LANGUAGE:-th} (default)}"
  _font="${PDF_FONT_OPT:-default}"
  _extra=""
  [ -n "$INSTRUCTIONS_OPT" ] && _extra=", with extra instructions (${#INSTRUCTIONS_OPT} chars)"
  echo "Summary: prompt ${PROMPT_NAME:-video (default)}, written in $_lang, PDF font $_font$_extra" >&2
  echo "Media: summary from $SUMMARY_SOURCE_EFF$([ "$SUMMARY_SOURCE_EFF" = voice ] && echo ' (transcript only, no frames)'); a meeting records $RECORD_MEDIA_EFF$([ "$RECORD_MEDIA_EFF" = audio ] && echo ' (an .m4a, summarized from the voice)')" >&2
  if [ "$DRY_BAD" -gt 0 ]; then
    echo "ERROR: $DRY_BAD input(s) can't be used (the bad/badarg/extra lines above)." >&2
    exit 1
  fi
  exit 0
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
    "${SUMMARY_INIT_ARGS[@]}"
  )
  # A new combine run takes the invocation's choice; a resumed one keeps its
  # own unless --summary-source was given (it is in SUMMARY_INIT_ARGS then).
  [ -z "$existing" ] && combine_init+=(--summary-source "$SUMMARY_SOURCE_EFF")
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
      # Prefix every line so concurrent runs stay readable — with a bash
      # read loop, not awk: mawk (Debian's awk) holds piped lines in its
      # input buffer. See prefix_lines in lib/run_one.sh.
      bash "$SCRIPT_DIR/lib/run_one.sh" "${args[@]}" 2>&1 \
        | while IFS= read -r _line || [ -n "$_line" ]; do
            printf '%s | %s\n' "$run_id" "$_line"
          done
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
