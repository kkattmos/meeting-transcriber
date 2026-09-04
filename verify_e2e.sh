#!/bin/bash
# Live end-to-end verification, for the real Debian 13 box with real keys.
#
# The two automated suites (lib/test_pipeline_e2e.sh, lib/test_media_e2e.sh)
# cover everything that can be checked without money or a meeting. Four things
# they cannot cover, because they need the real world:
#
#   * Chrome actually joining a live Google Meet / Zoom call
#   * real AssemblyAI / Anthropic / Gemini / youtube-transcript.io round trips
#   * Xvfb + PulseAudio + ffmpeg x11grab on this actual hardware
#   * the persistent Chrome profile still holding a valid Google/Zoom login
#
# This script is those checks. Run the preflight any time; run the recording
# checks when you have a call you can point the bot at.
#
#   ./verify_e2e.sh --preflight                     no API calls, no spend
#   ./verify_e2e.sh --browser-smoke                 real Chrome on Xvfb, recorded,
#                                                   but no meeting and no spend
#   ./verify_e2e.sh --mp4 /path/to/recording.mp4    real transcribe + summarize
#   ./verify_e2e.sh --youtube "<url>"               real captions + summarize
#   ./verify_e2e.sh --meet "<meet url>" --minutes 3
#   ./verify_e2e.sh --zoom "<zoom url>" --minutes 3
#   ./verify_e2e.sh --all --mp4 f --youtube u --meet m --zoom z
#
# The meeting checks record for --minutes (default 3) and then ask the bot to
# leave through the normal kill path, so they also verify the kill switch.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib/paths.sh"

export VERIFY_REPO="$SCRIPT_DIR"
PY="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}/bin/python3"
[ -x "$PY" ] || PY="python3"

PASS=0; FAIL=0; WARN=0
ok()   { PASS=$((PASS + 1)); echo "  ok    — $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL  — $1"; }
warn() { WARN=$((WARN + 1)); echo "  warn  — $1"; }

DO_PREFLIGHT=0
DO_BROWSER=0
MP4=""; YOUTUBE=""; MEET=""; ZOOM=""; MINUTES=3

while [ "$#" -gt 0 ]; do
  case "$1" in
    --preflight) DO_PREFLIGHT=1; shift ;;
    --browser-smoke) DO_BROWSER=1; shift ;;
    --mp4)       MP4="${2:-}"; shift 2 ;;
    --youtube)   YOUTUBE="${2:-}"; shift 2 ;;
    --meet)      MEET="${2:-}"; shift 2 ;;
    --zoom)      ZOOM="${2:-}"; shift 2 ;;
    --minutes)   MINUTES="${2:-3}"; shift 2 ;;
    --all)       DO_PREFLIGHT=1; DO_BROWSER=1; shift ;;
    -h|--help)   sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1 (try --help)" >&2; exit 1 ;;
  esac
done

if [ "$DO_PREFLIGHT" -eq 0 ] && [ "$DO_BROWSER" -eq 0 ] \
   && [ -z "$MP4$YOUTUBE$MEET$ZOOM" ]; then
  DO_PREFLIGHT=1
fi

run_id_for() {
  # The newest run whose input is this one.
  "$PY" "$SCRIPT_DIR/lib/runstate.py" find --root "$MEETING_BOT_ROOT/runs" \
        --input "$1" 2>/dev/null | head -n 1
}

check_run_artifacts() {
  local label="$1" input="$2" want_recording="$3"
  local run_id
  run_id="$(run_id_for "$input")"
  if [ -z "$run_id" ]; then
    bad "$label: no run was created for this input"
    return 1
  fi
  local run_dir="$MEETING_BOT_ROOT/runs/$run_id"
  echo "  run: $run_id"

  local stage
  for stage in transcribe frames summarize; do
    local status
    status="$("$PY" "$SCRIPT_DIR/lib/runstate.py" status --run-dir "$run_dir" \
              --stage "$stage")"
    [ "$status" = "done" ] && ok "$label: $stage done" \
                           || bad "$label: $stage is '$status'"
  done

  if [ "$want_recording" = "yes" ]; then
    if [ -s "$RECORDINGS_DIR/$run_id.mp4" ]; then
      local seconds
      seconds="$(ffprobe -v error -show_entries format=duration \
                 -of default=nw=1:nk=1 "$RECORDINGS_DIR/$run_id.mp4" 2>/dev/null)"
      ok "$label: recording is ${seconds:-?}s"
      # A recording with no audio stream means the sink wiring is wrong —
      # video looks fine and the transcript comes back empty.
      if ffprobe -v error -select_streams a -show_entries stream=codec_name \
           -of default=nw=1:nk=1 "$RECORDINGS_DIR/$run_id.mp4" 2>/dev/null \
           | grep -q .; then
        ok "$label: recording has an audio stream"
      else
        bad "$label: recording has NO audio stream (check the PulseAudio sink)"
      fi
    else
      bad "$label: no MP4 at $RECORDINGS_DIR/$run_id.mp4"
    fi
  fi

  local txt="$TRANSCRIPTS_DIR/$run_id.txt"
  if [ -s "$txt" ]; then
    ok "$label: transcript is $(wc -l < "$txt") line(s)"
    grep -qE '[^[:space:]]' "$txt" || bad "$label: transcript is blank"
  else
    bad "$label: no transcript at $txt"
  fi

  [ -s "$SUMMARIES_DIR/$run_id.md" ] && ok "$label: summary written" \
    || bad "$label: no summary at $SUMMARIES_DIR/$run_id.md"
  if [ -s "$PDF_DIR/$run_id.pdf" ]; then
    head -c 4 "$PDF_DIR/$run_id.pdf" | grep -q "%PDF" \
      && ok "$label: PDF written ($(stat -c %s "$PDF_DIR/$run_id.pdf") bytes)" \
      || bad "$label: $PDF_DIR/$run_id.pdf is not a PDF"
  else
    bad "$label: no PDF at $PDF_DIR/$run_id.pdf"
  fi
  # Which backend actually answered — on a fallback chain this is the only
  # way to find out after the fact.
  local model
  model="$(grep -m1 'model:' "$SUMMARIES_DIR/$run_id.md" 2>/dev/null | sed 's/.*model: *//')"
  [ -n "$model" ] && echo "  summarized by: $model"
}

# ---------------------------------------------------------------------------
if [ "$DO_PREFLIGHT" -eq 1 ]; then
echo ""
echo "=================================================================="
echo "Preflight — no API calls, nothing spent"
echo "=================================================================="

echo "--- host"
. /etc/os-release 2>/dev/null || true
[ "${ID:-}" = "debian" ] && ok "running on ${PRETTY_NAME:-Debian}" \
  || warn "this is ${PRETTY_NAME:-an unknown distro}, not Debian"

echo "--- programs"
for cmd in ffmpeg ffprobe Xvfb x11vnc websockify pactl pulseaudio yt-dlp \
           google-chrome-stable git pdftotext pdftoppm; do
  command -v "$cmd" >/dev/null 2>&1 && ok "$cmd" || bad "$cmd is missing (run ./setup.sh)"
done
command -v soffice >/dev/null 2>&1 || command -v libreoffice >/dev/null 2>&1 \
  && ok "libreoffice (optional: .pptx slide images)" \
  || warn "no libreoffice — .pptx resources contribute text but no slide images"

echo "--- python packages"
for mod in anthropic google.genai assemblyai requests playwright weasyprint markdown PIL; do
  "$PY" -c "import $mod" 2>/dev/null && ok "$mod" || bad "$mod not importable by $PY"
done

echo "--- output directories"
if paths_require; then
  for var in RECORDINGS_DIR TRANSCRIPTS_DIR FRAMES_DIR SUMMARIES_DIR PDF_DIR; do
    dir="${!var}"
    mkdir -p "$dir" 2>/dev/null
    if [ -d "$dir" ] && [ -w "$dir" ]; then
      ok "$var -> $dir (writable)"
    else
      bad "$var -> $dir is missing or not writable"
    fi
  done
else
  bad "the five output directories are not fully configured (see .env.example)"
fi

echo "--- API keys (counted, never printed)"
"$PY" "$SCRIPT_DIR/lib/keyring.py" status | sed 's/^/  /'
for spec in "ANTHROPIC_API_KEY:1" "ASSEMBLYAI_API_KEY:3" "YT_TRANSCRIPT_KEY:10"; do
  name="${spec%%:*}"; cap="${spec##*:}"
  count="$("$PY" - "$name" "$cap" <<'PYEOF'
import sys
sys.path.insert(0, __import__("os").path.join(
    __import__("os").environ.get("VERIFY_REPO", "."), "lib"))
from keyring import KeyRing
print(len(KeyRing.from_env(sys.argv[1], max_slots=int(sys.argv[2]))))
PYEOF
)"
  [ "${count:-0}" -ge 1 ] && ok "$name: $count key(s) configured" \
    || bad "$name: none configured"
done

echo "--- Chrome + the persistent login profile"
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-$MEETING_BOT_ROOT/chrome-profile}"
if command -v google-chrome-stable >/dev/null 2>&1; then
  ok "chrome $(google-chrome-stable --version 2>/dev/null | awk '{print $3}')"
fi
if [ -f "$CHROME_PROFILE_DIR/Default/Cookies" ]; then
  age_days=$(( ( $(date +%s) - $(stat -c %Y "$CHROME_PROFILE_DIR/Default/Cookies") ) / 86400 ))
  ok "login profile exists (cookies last written ${age_days}d ago)"
  [ "$age_days" -gt 25 ] && warn "the Google session may have expired — ./first_time_login.sh"
else
  bad "no login profile at $CHROME_PROFILE_DIR — run ./first_time_login.sh"
fi
if [ -e "$CHROME_PROFILE_DIR/SingletonLock" ]; then
  warn "a stale Chrome SingletonLock is present (it is removed automatically at launch)"
fi

echo "--- the display + audio + capture chain (a real 2-second recording)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib/xsession.sh"
SMOKE_MP4="$(mktemp -d)/smoke.mp4"
if DISPLAY_NUM="$(xsession_pick_display)"; then
  ok "claimed display :$DISPLAY_NUM"
  if xsession_start_xvfb "$DISPLAY_NUM" "${RECORD_GEOMETRY:-1920x1080}"; then
    ok "Xvfb started"
    SINK="$(xsession_sink_name "verify_$$")"
    if xsession_audio_start "$SINK"; then
      ok "PulseAudio sink '$SINK' created"
      if ffmpeg -y -loglevel error -f x11grab -video_size "${RECORD_GEOMETRY:-1920x1080}" \
           -framerate 10 -i ":$DISPLAY_NUM" -f pulse -i "${SINK}.monitor" \
           -t 2 -c:v libx264 -preset ultrafast -c:a aac "$SMOKE_MP4" 2>/dev/null \
         && [ -s "$SMOKE_MP4" ]; then
        ok "ffmpeg captured 2s of screen + sink audio ($(stat -c %s "$SMOKE_MP4") bytes)"
      else
        bad "ffmpeg could not capture from :$DISPLAY_NUM / ${SINK}.monitor"
      fi
      xsession_audio_stop "$SINK"
    else
      bad "could not create a PulseAudio sink"
    fi
    xsession_stop_xvfb
  else
    bad "Xvfb would not start"
  fi
else
  bad "no free X display"
fi
rm -rf "$(dirname "$SMOKE_MP4")"
fi

# ---------------------------------------------------------------------------
if [ "$DO_BROWSER" -eq 1 ]; then
echo ""
echo "=================================================================="
echo "Browser smoke — real Chrome, recorded, without a meeting"
echo "=================================================================="
# Everything stage 1 does except joining a call: Chrome under Xvfb through
# Playwright with the recorder's own flags, its audio in this run's sink, and
# ffmpeg capturing the result. If this passes, a failure to record a real
# meeting is about the meeting, not about the machine.
# shellcheck disable=SC1091
. "$SCRIPT_DIR/lib/xsession.sh"
SMOKE_DIR="$(mktemp -d)"
SMOKE_MP4="$SMOKE_DIR/browser.mp4"
SMOKE_PNG="$SMOKE_DIR/browser.png"
GEOM="${RECORD_GEOMETRY:-1920x1080}"

if ! command -v google-chrome-stable >/dev/null 2>&1; then
  bad "browser: google-chrome-stable is not installed"
elif ! "$PY" -c "import playwright" 2>/dev/null; then
  bad "browser: playwright is not installed in $PY"
else
  DISPLAY_NUM="$(xsession_pick_display)" || DISPLAY_NUM=""
  if [ -z "$DISPLAY_NUM" ]; then
    bad "browser: no free display"
  else
    SINK="$(xsession_sink_name "smoke_$$")"
    smoke_cleanup() {
      [ -n "${SMOKE_FFMPEG:-}" ] && kill -INT "$SMOKE_FFMPEG" 2>/dev/null || true
      xsession_audio_stop "$SINK" || true
      xsession_stop_xvfb || true
      true
    }
    if xsession_start_xvfb "$DISPLAY_NUM" "$GEOM" && xsession_audio_start "$SINK"; then
      export DISPLAY=":$DISPLAY_NUM"
      export PULSE_SINK="$SINK"
      ffmpeg -y -loglevel error -f x11grab -video_size "$GEOM" -framerate 10 \
        -i ":$DISPLAY_NUM" -f pulse -i "${SINK}.monitor" \
        -c:v libx264 -preset ultrafast -crf 28 -c:a aac -pix_fmt yuv420p \
        -t 8 "$SMOKE_MP4" >/dev/null 2>&1 &
      SMOKE_FFMPEG=$!

      if "$PY" "$SCRIPT_DIR/screen/browser_smoke.py" --seconds 5 \
           --screenshot "$SMOKE_PNG" > "$SMOKE_DIR/smoke.log" 2>&1; then
        ok "browser: Chrome launched via Playwright with the recorder's flags"
        WIN="$(grep -o 'Window is [0-9]*x[0-9]*' "$SMOKE_DIR/smoke.log" | awk '{print $3}')"
        echo "  viewport: ${WIN:-unknown} (head is $GEOM; 1px under is normal)"
        [ -s "$SMOKE_PNG" ] && ok "browser: page screenshot captured" \
          || warn "browser: no screenshot written"
      else
        bad "browser: Chrome failed to launch — see $SMOKE_DIR/smoke.log"
        sed 's/^/    /' "$SMOKE_DIR/smoke.log" | tail -n 15
      fi

      wait "$SMOKE_FFMPEG" 2>/dev/null
      SMOKE_FFMPEG=""
      if [ -s "$SMOKE_MP4" ]; then
        ok "browser: recorded $(stat -c %s "$SMOKE_MP4") bytes of the live browser"
        ffprobe -v error -select_streams a -show_entries stream=codec_name \
          -of default=nw=1:nk=1 "$SMOKE_MP4" 2>/dev/null | grep -q . \
          && ok "browser: the recording has an audio stream" \
          || bad "browser: the recording has no audio stream"
        # A capture of a blank display compresses to almost nothing; a real
        # browser window does not. This is the check that catches "Chrome
        # started but rendered nowhere".
        if [ "$(stat -c %s "$SMOKE_MP4")" -gt 20000 ]; then
          ok "browser: the capture has real picture content in it"
        else
          bad "browser: the capture is suspiciously small — Chrome may not have rendered"
        fi
        # The check that actually matters: measure the recorded frame for the
        # black bands a mispositioned or mis-sized window leaves behind. This
        # is how the missing --window-position=0,0 was found; comparing window
        # sizes would not have caught it.
        ffmpeg -y -loglevel error -ss 3 -i "$SMOKE_MP4" -frames:v 1 \
          "$SMOKE_DIR/frame.png" 2>/dev/null
        if [ -s "$SMOKE_DIR/frame.png" ] && "$PY" -c "import PIL" 2>/dev/null; then
          BANDS="$("$PY" - "$SMOKE_DIR/frame.png" <<'PYEOF'
from PIL import Image
import sys
img = Image.open(sys.argv[1]).convert("RGB")
px, (w, h) = img.load(), img.size
lit = lambda vals: max(sum(v) / 3 for v in vals)
row = lambda y: lit([px[x, y] for x in range(w)])
col = lambda x: lit([px[x, y] for y in range(h)])
def first(rng, fn):
    for i in rng:
        if fn(i) > 4:
            return i
    return -1
top = first(range(h), row); bottom = first(range(h - 1, -1, -1), row)
left = first(range(w), col); right = first(range(w - 1, -1, -1), col)
if min(top, bottom, left, right) < 0:
    print("blank")
else:
    print(f"{left} {top} {w - 1 - right} {h - 1 - bottom}")
PYEOF
)"
          if [ "$BANDS" = "blank" ]; then
            bad "browser: the captured frame is entirely black"
          elif [ "$(echo "$BANDS" | tr " " "\n" | sort -rn | head -1)" -le 2 ]; then
            # Chrome's kiosk viewport comes out a pixel under the window size,
            # so a 1px line at the right and bottom is expected and invisible.
            # Anything thicker means the window is genuinely misplaced — that
            # is what the missing --window-position=0,0 looked like (10px).
            ok "browser: the browser fills the display (bands $BANDS, within 2px)"
          else
            bad "browser: black bands (left top right bottom) = $BANDS — the window does not fill the head"
          fi
        else
          warn "browser: could not measure black edges (need Pillow + a decodable frame)"
        fi
      else
        bad "browser: ffmpeg produced no file"
      fi
      smoke_cleanup
    else
      bad "browser: could not bring up the display or the audio sink"
      smoke_cleanup
    fi
  fi
fi
echo "  artifacts: $SMOKE_DIR"
fi

# ---------------------------------------------------------------------------
if [ -n "$MP4" ]; then
echo ""
echo "=================================================================="
echo "Local MP4 — real AssemblyAI + real summarizer"
echo "=================================================================="
if [ ! -f "$MP4" ]; then
  bad "no such file: $MP4"
else
  echo "  input: $MP4"
  bash "$SCRIPT_DIR/pipeline.sh" "$MP4" 2>&1 | tail -n 25
  check_run_artifacts "mp4" "$MP4" "no"
fi
fi

if [ -n "$YOUTUBE" ]; then
echo ""
echo "=================================================================="
echo "YouTube — real captions API + real summarizer"
echo "=================================================================="
echo "  input: $YOUTUBE"
bash "$SCRIPT_DIR/pipeline.sh" "$YOUTUBE" 2>&1 | tail -n 25
check_run_artifacts "youtube" "$YOUTUBE" "no"
fi

record_meeting() {
  local label="$1" url="$2"
  echo ""
  echo "=================================================================="
  echo "$label — real Chrome joining a live call for ${MINUTES} minute(s)"
  echo "=================================================================="
  echo "  input: $url"
  echo "  Admit the bot if the call has a waiting room; it leaves on its own"
  echo "  after ${MINUTES} minute(s) via the normal kill path."

  bash "$SCRIPT_DIR/pipeline.sh" "$url" --name "verify_${label}" > \
    "$MEETING_BOT_ROOT/verify_${label}.log" 2>&1 &
  local pipeline_pid=$!

  # Wait for the run to appear and be admitted, then let it record.
  local run_id="" waited=0
  while [ -z "$run_id" ] && [ "$waited" -lt 120 ]; do
    run_id="$(run_id_for "$url")"
    sleep 2; waited=$((waited + 2))
  done
  if [ -z "$run_id" ]; then
    bad "$label: no run appeared within 120s"
    kill "$pipeline_pid" 2>/dev/null
    return 1
  fi
  ok "$label: run $run_id started"

  waited=0
  while [ ! -f "$MEETING_BOT_ROOT/runs/$run_id/admitted" ] && [ "$waited" -lt 300 ]; do
    kill -0 "$pipeline_pid" 2>/dev/null || break
    sleep 5; waited=$((waited + 5))
  done
  if [ -f "$MEETING_BOT_ROOT/runs/$run_id/admitted" ]; then
    ok "$label: the bot was admitted to the call"
  else
    bad "$label: never admitted (see $MEETING_BOT_ROOT/runs/$run_id/logs/record.log)"
  fi

  echo "  recording for ${MINUTES} minute(s)..."
  sleep $((MINUTES * 60))

  echo "  asking the bot to leave (this also tests the kill switch)"
  bash "$SCRIPT_DIR/kill_meeting.sh" --run-id "$run_id" 2>&1 | sed 's/^/  /'
  wait "$pipeline_pid"
  local rc=$?
  [ "$rc" -eq 0 ] && ok "$label: pipeline finished cleanly" \
                  || warn "$label: pipeline exited $rc — resume with ./pipeline.sh --run-id $run_id"
  check_run_artifacts "$label" "$url" "yes"
}

[ -n "$MEET" ] && record_meeting "meet" "$MEET"
[ -n "$ZOOM" ] && record_meeting "zoom" "$ZOOM"

echo ""
echo "=================================================================="
echo "Result: $PASS passed, $FAIL failed, $WARN warning(s)"
echo "=================================================================="
[ "$FAIL" -eq 0 ] || exit 1
