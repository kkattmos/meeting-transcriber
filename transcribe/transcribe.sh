#!/bin/bash
# Option 2: Transcribe an audio file (WAV/MP4/M4A/MKV/WEBM/OGG) or a YouTube
# URL to text.
#
# Routing:
#   * YouTube URL  -> youtube-transcript.io API (fast, multi-account
#                     round-robin via yt_transcript_client.py). The argument
#                     is the URL itself; the client extracts the video id.
#                     No audio is downloaded, no AssemblyAI is run.
#   * Kaltura embed-> the entry's own caption track when it has one (free and
#                     instant, same reasoning as the YouTube path), otherwise
#                     AssemblyAI on the MP4 given behind --media. The input is
#                     the <iframe> tag or its src URL; lib/kaltura.py parses it.
#   * Local file   -> AssemblyAI pre-recorded transcription API
#                     (transcribe/assemblyai_client.py). MP4/M4A/WAV are
#                     sent directly; WEBM/OGG are first demuxed to MP3 with
#                     ffmpeg. Audio and slides are recorded together by
#                     screen/record_screen.sh into one MP4.
#
# Empty-transcript behaviour: if either backend returns no usable transcript
# (no captions on YouTube, or empty response from AssemblyAI), the script
# fails loudly with exit code 2. Every key/account hits the same upstream
# model/data, so retrying with a different key won't help — investigate
# (region, login state, captions availability, audio quality) and re-run.
#
# Usage:
#   ./transcribe/transcribe.sh <file_or_url> "<name>" [language] [--out-base PATH] \
#                              [--media PATH] [--clip-captions WINDOW]
#
# --media PATH is the already-downloaded media file to fall back to when a URL
# input turns out to have no captions. The pipeline passes it on the Kaltura
# path, where the download has happened before this stage runs anyway.
#
# --clip-captions WINDOW ("00:05:00-01:30:00") trims a CAPTION-DERIVED
# transcript to that window and shifts it to clip-relative time. It is
# deliberately named for what it does: it has no effect on the AssemblyAI path,
# because a media file is windowed by cutting the media (lib/clip.py cut) —
# which is what the pipeline does before this stage runs, so that AssemblyAI
# only bills the minutes asked for. Applying a window here as well would take a
# second slice out of the first one. If you are calling this script directly on
# a media file and want part of it, cut it first:
#     lib/clip.py cut lecture.mp4 /tmp/clip.mp4 00:05:00-01:30:00
#
# Language is an ISO-639-1 code AssemblyAI recognises: "th" (Thai, default),
# "en", "auto", or any AssemblyAI language code. Ignored on the YouTube path
# because captions come pre-transcribed.
#
# Output:
#   $TRANSCRIPTS_DIR/<name>_<timestamp>.{txt,srt}
#
# --out-base PATH overrides that with PATH.txt / PATH.srt. The pipeline passes
# it so the output path is a function of the run id rather than of the clock:
# a resumed run has to land on the same filenames the first attempt used, or it
# can't tell what already succeeded.
set -e

# Load `.env` from the repo root (one file for every stage). No-op if no
# `.env` exists. Already-set env vars take precedence so one-off overrides
# via `KEY=val ./transcribe.sh ...` continue to work.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck disable=SC1091
. "$ROOT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$ROOT_DIR/lib/paths.sh"

OUT_BASE=""
MEDIA_FALLBACK=""
CLIP_CAPTIONS=""
declare -a ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --out-base) OUT_BASE="${2:-}"; shift 2 ;;
    --media)    MEDIA_FALLBACK="${2:-}"; shift 2 ;;
    --clip-captions) CLIP_CAPTIONS="${2:-}"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]:-}"

if [ -z "${1:-}" ] || [ -z "${2:-}" ]; then
  echo "Usage: $0 <file_or_url> <name> [language] [--out-base PATH] [--media PATH] [--clip-captions W]"
  echo "  language: th (default), en, auto, or any AssemblyAI language code"
  exit 1
fi

INPUT="$1"
MEETING_NAME="${2:-meeting}"
# CLI arg overrides ASSEMBLYAI_LANGUAGE from .env, which overrides the default.
LANGUAGE="${3:-${ASSEMBLYAI_LANGUAGE:-th}}"

STAMP=$(date +%Y%m%d_%H%M%S)
SAFE_NAME=$(echo "$MEETING_NAME" | tr ' ' '_' | tr -cd 'A-Za-z0-9_-')
if [ -n "$OUT_BASE" ]; then
  OUTPUT_BASE="$OUT_BASE"
  TRANSCRIPT_DIR="$(dirname "$OUTPUT_BASE")"
else
  paths_require TRANSCRIPTS_DIR || exit 1
  TRANSCRIPT_DIR="$TRANSCRIPTS_DIR"
  OUTPUT_BASE="${TRANSCRIPT_DIR}/${SAFE_NAME}_${STAMP}"
fi
# Under MEETING_BOT_ROOT rather than /tmp: demuxing a long WEBM writes an MP3
# here, and /tmp is a small tmpfs on this box.
WORK_DIR="${MEETING_BOT_ROOT}/tmp/transcribe-$$"
mkdir -p "$TRANSCRIPT_DIR" "$WORK_DIR"

# YouTube URLs are detected by URL pattern, NOT by file extension - the input
# is a URL string, not a file.
IS_YOUTUBE=0
if echo "$INPUT" | grep -qE '(youtube\.com/watch\?v=|youtu\.be/)'; then
  IS_YOUTUBE=1
fi

# Same venv used by the summarize step. Falls back to system python3 if the
# venv is missing (e.g. user is running on a system where setup.sh hasn't
# run).
PYTHON_BIN="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}/bin/python3"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

# Kaltura is recognised by the same parser the pipeline classifies with, so an
# input accepted there can't be rejected here. Checked after YouTube (the two
# never overlap) and with $PYTHON_BIN, because kaltura.py imports requests and
# only the venv is guaranteed to have it.
IS_KALTURA=0
if [ "$IS_YOUTUBE" -eq 0 ] && [ ! -f "$INPUT" ] \
   && "$PYTHON_BIN" "$ROOT_DIR/lib/kaltura.py" parse "$INPUT" >/dev/null 2>&1; then
  IS_KALTURA=1
fi

SEGMENTS_FILE=""
# Whether the segments came from a caption track rather than from AssemblyAI.
# --clip-captions acts on exactly this case and no other; see the header.
SEGMENTS_ARE_CAPTIONS=0

# --- YouTube path: youtube-transcript.io API -------------------------------
if [ "$IS_YOUTUBE" -eq 1 ]; then
  echo "==> Fetching YouTube transcript via youtube-transcript.io"
  SEGMENTS_FILE="$WORK_DIR/segments.json"
  # $LANGUAGE only picks among the caption tracks the video already has —
  # nothing is translated or transcribed here.
  if ! "$PYTHON_BIN" "$SCRIPT_DIR/yt_transcript_client.py" "$INPUT" "$LANGUAGE" \
        > "$SEGMENTS_FILE" 2>"$WORK_DIR/yt-client.log"; then
    cat "$WORK_DIR/yt-client.log"
    echo "ERROR: youtube-transcript.io fetch failed (see $WORK_DIR/yt-client.log)"
    rm -rf "$WORK_DIR"
    exit 1
  fi
  SEGMENT_COUNT=$(grep -c '"text"' "$SEGMENTS_FILE" || true)
  if [ "${SEGMENT_COUNT:-0}" -lt 1 ]; then
    echo "ERROR: youtube-transcript.io returned no usable transcript."
    echo "       The video may have no captions or contain only placeholders."
    echo "       Investigate (region, login state, captions availability) and re-run."
    rm -rf "$WORK_DIR"
    exit 2
  fi
  SEGMENTS_ARE_CAPTIONS=1
else
  # The file AssemblyAI will be given. Normally the input itself; on the
  # Kaltura path the input is a URL and this becomes --media instead.
  MEDIA_INPUT="$INPUT"
  HAVE_SEGMENTS=0

  # --- Kaltura path: the entry's own captions, when it has any -------------
  # Free and instant, so it is always tried before paying AssemblyAI. Most
  # lecture-capture entries have no caption track at all, and exit code 3 says
  # exactly that — anything else is a real failure and must not be swallowed
  # into an expensive fallback.
  if [ "$IS_KALTURA" -eq 1 ]; then
    echo "==> Checking the Kaltura entry for captions"
    SEGMENTS_FILE="$WORK_DIR/segments.json"
    set +e
    "$PYTHON_BIN" "$ROOT_DIR/lib/kaltura.py" captions "$INPUT" "$LANGUAGE" \
      > "$SEGMENTS_FILE" 2>"$WORK_DIR/kaltura-client.log"
    KAL_RC=$?
    set -e
    case "$KAL_RC" in
      0)
        echo "    using the entry's caption track — skipping AssemblyAI"
        HAVE_SEGMENTS=1
        SEGMENTS_ARE_CAPTIONS=1
        ;;
      3)
        echo "    no caption track on this entry — transcribing the audio instead"
        rm -f "$SEGMENTS_FILE"
        SEGMENTS_FILE=""
        if [ -z "$MEDIA_FALLBACK" ]; then
          cat "$WORK_DIR/kaltura-client.log"
          echo "ERROR: this Kaltura entry has no captions and no --media file was"
          echo "       given, so there is nothing to send to AssemblyAI."
          echo "       The pipeline passes the downloaded MP4; if you are calling"
          echo "       this script directly, download it first:"
          echo "         lib/kaltura.py download '<input>' /tmp/lecture.mp4"
          rm -rf "$WORK_DIR"
          exit 1
        fi
        MEDIA_INPUT="$MEDIA_FALLBACK"
        ;;
      *)
        cat "$WORK_DIR/kaltura-client.log"
        echo "ERROR: could not read the Kaltura entry (see $WORK_DIR/kaltura-client.log)"
        rm -rf "$WORK_DIR"
        exit "$KAL_RC"
        ;;
    esac
  fi

  # Skipped entirely when those captions came back: the entry's own transcript
  # makes this whole half free.
  if [ "$HAVE_SEGMENTS" -eq 0 ]; then
    # --- Local file path: AssemblyAI pre-recorded API ------------------------
    # AssemblyAI accepts mp3, mp4, m4a, wav directly. WEBM and OGG need to be
    # demuxed to mp3 first because the SDK's upload helper occasionally chokes
    # on those containers. ffmpeg is available because setup.sh installs it.
    if [ ! -f "$MEDIA_INPUT" ]; then
      echo "Input file not found: $MEDIA_INPUT"
      rm -rf "$WORK_DIR"
      exit 1
    fi
    EXT="${MEDIA_INPUT##*.}"
    EXT_LOWER=$(echo "$EXT" | tr '[:upper:]' '[:lower:]')
    case "$EXT_LOWER" in
      mp3|mp4|m4a|wav)
        AUDIO_FILE="$MEDIA_INPUT"
        ;;
      webm|ogg)
        echo "==> Demuxing $EXT_LOWER -> MP3 for AssemblyAI"
        AUDIO_FILE="$WORK_DIR/audio.mp3"
        ffmpeg -y -i "$MEDIA_INPUT" -vn -acodec libmp3lame -b:a 128k "$AUDIO_FILE" \
          > "$WORK_DIR/ffmpeg.log" 2>&1
        ;;
      *)
        echo "Unsupported input extension: .$EXT_LOWER"
        echo "Supported: .wav, .mp3, .mp4, .m4a, .webm, .ogg, or a YouTube URL"
        rm -rf "$WORK_DIR"
        exit 1
        ;;
    esac

    echo "==> Transcribing with AssemblyAI (language: $LANGUAGE)"
    SEGMENTS_FILE="$WORK_DIR/segments.json"
    # Run it plainly and capture $? on the next line. Inside `if ! cmd; then`,
    # $? is the status of the *negated* condition — i.e. always 0 — so the old
    # `exit "$EXIT_CODE"` here exited 0 on failure. transcribe.sh then looked
    # successful, run_one.sh marked the stage done, and it recorded .txt/.srt
    # artifacts that had never been written. (runstate's on-disk artifact check
    # caught it after the fact, but only on the next resume.)
    "$PYTHON_BIN" "$SCRIPT_DIR/assemblyai_client.py" "$AUDIO_FILE" "$LANGUAGE" \
          > "$SEGMENTS_FILE" 2>"$WORK_DIR/assemblyai-client.log"
    EXIT_CODE=$?
    if [ "$EXIT_CODE" -ne 0 ]; then
      cat "$WORK_DIR/assemblyai-client.log"
      echo "ERROR: AssemblyAI transcription failed (see $WORK_DIR/assemblyai-client.log)"
      rm -rf "$WORK_DIR"
      exit "$EXIT_CODE"
    fi
  fi
fi

# --- Caption window --------------------------------------------------------
# Captions arrive whole and free, so there is no media to cut on this path and
# the window is applied to the segments instead — by lib/clip.py, the same code
# that parses it for the pipeline, so the two halves can never disagree about
# what "00:05:00" means. Segments come out rebased to the clip, matching the
# timebase a cut media file would have produced.
if [ -n "$CLIP_CAPTIONS" ] && [ "$SEGMENTS_ARE_CAPTIONS" -eq 1 ]; then
  echo "==> Trimming the captions to $CLIP_CAPTIONS"
  if ! "$PYTHON_BIN" "$ROOT_DIR/lib/clip.py" segments "$CLIP_CAPTIONS" \
        < "$SEGMENTS_FILE" > "$WORK_DIR/segments-clipped.json"; then
    echo "ERROR: could not apply the clip window $CLIP_CAPTIONS"
    rm -rf "$WORK_DIR"
    exit 1
  fi
  mv "$WORK_DIR/segments-clipped.json" "$SEGMENTS_FILE"
  # An empty result is a real failure, not an empty transcript: it means the
  # window falls outside the video, and every later stage would go on to
  # summarize nothing at all.
  if ! grep -q '"text"' "$SEGMENTS_FILE"; then
    echo "ERROR: no transcript falls inside $CLIP_CAPTIONS."
    echo "       The window is probably past the end of the video."
    rm -rf "$WORK_DIR"
    exit 2
  fi
fi

# --- Shared writer: convert segments.json -> .txt + .srt -------------------
# Both the YouTube and AssemblyAI backends emit the same JSON shape
# (list of {text, offset_ms, duration_ms}), so a single heredoc-driven
# writer covers both. Keeping it in one place means changes to the
# output format only need to be made once.
echo "==> Writing transcript + subtitles"
"$PYTHON_BIN" - "$SEGMENTS_FILE" "$OUTPUT_BASE" <<'PYEOF'
import json, sys
from pathlib import Path
segments = json.loads(Path(sys.argv[1]).read_text())
base = sys.argv[2]
txt = Path(base + ".txt")
srt = Path(base + ".srt")
def fmt(ms):
    s, ms = divmod(int(ms), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
txt.write_text("\n".join(seg["text"] for seg in segments) + "\n")
chunks = []
for i, seg in enumerate(segments, start=1):
    start = seg["offset_ms"]
    end = start + max(seg["duration_ms"], 1)
    chunks.append(f"{i}\n{fmt(start)} --> {fmt(end)}\n{seg['text']}\n")
srt.write_text("\n".join(chunks))
print(f"  -> {txt} ({len(segments)} segments)")
print(f"  -> {srt}")
PYEOF

# --- Cleanup -----------------------------------------------------------------
# Always remove the workdir, even on the YouTube path which creates one. The
# heredoc writer doesn't touch it, so we know it's safe to drop here.
rm -rf "$WORK_DIR"

echo "==> Done."
echo "Transcript: ${OUTPUT_BASE}.txt"
echo "Subtitles:  ${OUTPUT_BASE}.srt"
