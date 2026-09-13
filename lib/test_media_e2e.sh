#!/bin/bash
# End-to-end test over REAL media, with only the paid APIs stubbed.
#
# test_pipeline_e2e.sh stubs the four expensive stages to test the
# orchestration. This one does the opposite: it runs the stages themselves for
# real — ffmpeg frame extraction on a genuine MP4, the real AssemblyAI SDK, the
# real anthropic SDK, the real chunker, the real document wrapper, the real
# WeasyPrint render — against local stub servers (lib/fake_api_server.py) that
# speak the providers' HTTP protocols.
#
# So it covers, without a single API key:
#   * a local .mp4 all the way to a .md and a .pdf
#   * a YouTube URL through the captions client to the same outputs
#   * that the Messages request really carries output_config.effort, adaptive
#     thinking and the frame images
#   * that --resources material reaches the model and the PDF
#   * that key rotation advances the on-disk cursor
#
# It does NOT prove Chrome joins a live meeting, or that the real providers
# accept our requests — see verify_e2e.sh for the checks that need the real
# box and real keys.
#
#     ./lib/test_media_e2e.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export REPO_DIR="$REPO"
# A space in the path, on purpose — see test_pipeline_e2e.sh.
TESTROOT="$(mktemp -d)/media root"
mkdir -p "$TESTROOT"
PASS=0
FAIL=0

PY="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}/bin/python3"
[ -x "$PY" ] || PY="python3"

ok()   { PASS=$((PASS + 1)); echo "  ok   — $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL — $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }

SERVER_PIDS=()
cleanup() {
  for pid in "${SERVER_PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  rm -rf "$(dirname "$TESTROOT")"
  true
}
[ -n "${KEEP_TESTROOT:-}" ] && echo "TESTROOT=$TESTROOT" || trap cleanup EXIT

# --- Preconditions -----------------------------------------------------------
missing=""
command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
"$PY" -c "import assemblyai" 2>/dev/null || missing="$missing assemblyai"
"$PY" -c "import requests" 2>/dev/null || missing="$missing requests"
if [ -n "$missing" ]; then
  echo "SKIP: missing dependencies:$missing"
  echo "  Run ./setup.sh (or pip install them into \$MEETING_BOT_VENV) first."
  exit 0
fi
HAVE_PDF=1
"$PY" -c "import weasyprint, markdown, PIL" 2>/dev/null || HAVE_PDF=0

# --- Environment -------------------------------------------------------------
export MEETING_BOT_ROOT="$TESTROOT/opt"
export RECORDINGS_DIR="$TESTROOT/opt/recordings"
export TRANSCRIPTS_DIR="$TESTROOT/opt/transcripts"
export FRAMES_DIR="$TESTROOT/opt/frames"
export SUMMARIES_DIR="$TESTROOT/opt/summaries"
export PDF_DIR="$TESTROOT/opt/pdf"
export RESOURCE_CACHE_DIR="$TESTROOT/opt/resources"
mkdir -p "$RECORDINGS_DIR" "$TRANSCRIPTS_DIR" "$FRAMES_DIR" "$SUMMARIES_DIR" \
         "$PDF_DIR" "$RESOURCE_CACHE_DIR" "$MEETING_BOT_ROOT/state"

# Three keys each, so rotation has something to rotate.
export ASSEMBLYAI_API_KEY_1="stub-aai-1"
export ASSEMBLYAI_API_KEY_2="stub-aai-2"
export ASSEMBLYAI_API_KEY_3="stub-aai-3"
export YT_TRANSCRIPT_KEY_1="stub-yt-1"
export YT_TRANSCRIPT_KEY_2="stub-yt-2"
export SUMMARY_BACKEND=claude-cli
export SUMMARY_EFFORT=medium
export CLAUDE_CLI_MODEL=opus

# The summarizer spends a Claude subscription through `claude -p`, so this
# seam is a stub executable rather than a stub HTTP server. See
# lib/fake_claude_cli.py for what it records and why.
CLI_RECORD="$TESTROOT/claude_cli.jsonl"
export FAKE_CLAUDE_RECORD="$CLI_RECORD"
export CLAUDE_CLI_BIN="$REPO/lib/fake_claude_cli.py"

# Deliberately set here, and deliberately expected to be absent from the
# child's environment: llm_client has to strip it, or a run silently bills a
# metered API account instead of the subscription. Asserted in section 4.
export ANTHROPIC_API_KEY="must-not-reach-the-cli"
export ASSEMBLYAI_LANGUAGE=th
export ASSEMBLYAI_POLL_SECONDS=0.2
export FRAME_PERIOD_SECONDS=5
export SCENE_THRESHOLD=0.2

RECORD_FILE="$TESTROOT/requests.jsonl"

start_stub() {
  local which="$1" port="$2"
  "$PY" "$REPO/lib/fake_api_server.py" --which "$which" --port "$port" \
        --record "$RECORD_FILE" >/dev/null 2>&1 &
  SERVER_PIDS+=($!)
  # Wait for the socket rather than sleeping: a slow box would otherwise fail
  # the first request and look like a broken client.
  for _ in $(seq 1 50); do
    if "$PY" - "$port" <<'PYEOF' 2>/dev/null
import socket, sys
s = socket.socket()
s.settimeout(0.2)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PYEOF
    then return 0; fi
    sleep 0.2
  done
  echo "ERROR: stub $which did not start on port $port" >&2
  return 1
}

start_stub assemblyai 8802 || exit 1
start_stub youtube 8803 || exit 1
export ASSEMBLYAI_BASE_URL="http://127.0.0.1:8802"
export YT_TRANSCRIPT_API_URL="http://127.0.0.1:8803/api/transcripts"

echo ""
echo "=================================================================="
echo "1. Build a real lecture-shaped MP4"
echo "=================================================================="
LECTURE="$TESTROOT/Week 4 Lecture.mp4"
# Three "slides": a bright panel on a dark background, changing colour twice —
# scene-change detection has something real to find, and framecrop has a real
# slide region to crop to.
ffmpeg -y -loglevel error \
  -f lavfi -i "color=c=0x101014:s=960x540:d=30" \
  -f lavfi -i "sine=frequency=440:duration=30" \
  -filter_complex "[0:v]drawbox=x=80:y=50:w=800:h=440:color=0xf5f5f0:t=fill:enable='between(t,0,9)',\
drawbox=x=80:y=50:w=800:h=440:color=0xf0e8d8:t=fill:enable='between(t,10,19)',\
drawbox=x=80:y=50:w=800:h=440:color=0xe8f0f5:t=fill:enable='between(t,20,30)'[v]" \
  -map "[v]" -map 1:a -c:v libx264 -preset ultrafast -crf 28 -pix_fmt yuv420p \
  -c:a aac -b:a 64k -t 30 "$LECTURE" 2>"$TESTROOT/ffmpeg.log"
[ -s "$LECTURE" ] && ok "built a 30s MP4 with slides and audio" \
  || { bad "could not build the test MP4 (see $TESTROOT/ffmpeg.log)"; exit 1; }

echo ""
echo "=================================================================="
echo "2. Frame extraction (real ffmpeg)"
echo "=================================================================="
FRAME_OUT="$FRAMES_DIR/week4"
"$PY" "$REPO/screen/extract_frames.py" "$LECTURE" "$FRAME_OUT" "week4" \
  > "$TESTROOT/frames.log" 2>&1
check "extract_frames exits 0" "$?" "0"
[ -f "$FRAME_OUT/manifest.json" ] && ok "manifest written" || bad "no manifest"
FRAME_COUNT=$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['frame_count'])" \
              "$FRAME_OUT/manifest.json" 2>/dev/null)
[ "${FRAME_COUNT:-0}" -ge 3 ] && ok "extracted $FRAME_COUNT frames" \
  || bad "expected several frames, got ${FRAME_COUNT:-0}"
JPEGS=$(find "$FRAME_OUT" -name '*.jpg' | wc -l)
[ "$JPEGS" -ge 3 ] && ok "$JPEGS JPEGs on disk" || bad "frames missing from disk"

echo ""
echo "=================================================================="
echo "2b. The clip window, cut out of the real MP4"
echo "=================================================================="
# lib/test_clip.py asserts the ffmpeg COMMAND. This is the other half: that the
# command actually produces a playable file whose timestamps start at zero.
# Everything about --clip rests on that rebasing — without it the clip's first
# frame is still stamped at the source offset, and every SRT cue and frame
# timestamp downstream silently inherits it.
CLIPPED="$TESTROOT/clip.mp4"
"$PY" "$REPO/lib/clip.py" cut "$LECTURE" "$CLIPPED" "00:00:10-00:00:20" \
  > "$TESTROOT/clip.log" 2>&1
check "clip.py cut exits 0" "$?" "0"
[ -s "$CLIPPED" ] && ok "clip.mp4 written" || bad "no clip written"
[ -f "$CLIPPED.part" ] && bad "left a .part behind" || ok "no .part left behind"

probe() {
  ffprobe -v error -show_entries "format=$1" -of default=nw=1:nk=1 "$2" 2>/dev/null
}
CLIP_DURATION=$(probe duration "$CLIPPED")
CLIP_START=$(probe start_time "$CLIPPED")
"$PY" -c "import sys; d=float(sys.argv[1]); sys.exit(0 if 8.0 <= d <= 12.0 else 1)" \
  "${CLIP_DURATION:-0}" \
  && ok "the clip is ~10s long (${CLIP_DURATION}s)" \
  || bad "expected a ~10s clip, got ${CLIP_DURATION:-none}s"
# Stream-copy lands on the preceding keyframe, so allow a GOP of slack — the
# assertion is that it is near ZERO, not near 10, which is what would happen
# if -avoid_negative_ts were dropped.
"$PY" -c "import sys; t=float(sys.argv[1]); sys.exit(0 if abs(t) < 2.0 else 1)" \
  "${CLIP_START:-99}" \
  && ok "the clip starts at t=0 (${CLIP_START}s), not at the source offset" \
  || bad "clip timestamps were not rebased: start_time=${CLIP_START:-none}"

echo "--- frames extracted from the clip carry clip-relative timestamps"
CLIP_FRAMES="$FRAMES_DIR/week4-clip"
# A short period, so the assertion is about WHERE the frames are rather than
# about whether a 10s window happened to contain a scene change.
FRAME_PERIOD_SECONDS=2 "$PY" "$REPO/screen/extract_frames.py" \
  "$CLIPPED" "$CLIP_FRAMES" "week4-clip" > "$TESTROOT/clip-frames.log" 2>&1
check "extract_frames on the clip exits 0" "$?" "0"
LAST_TS=$("$PY" -c "
import json,sys
frames = json.load(open(sys.argv[1]))['frames']
# 999 for an empty manifest, so 'no frames at all' fails this check rather
# than passing it vacuously.
print(max(f['timestamp_s'] for f in frames) if frames else 999)
" "$CLIP_FRAMES/manifest.json" 2>/dev/null)
"$PY" -c "import sys; sys.exit(0 if float(sys.argv[1]) <= 12.0 else 1)" "${LAST_TS:-999}" \
  && ok "the last frame is at ${LAST_TS}s, inside the clip" \
  || bad "frame timestamps are source-relative: last frame at ${LAST_TS:-none}s"

echo ""
echo "=================================================================="
echo "3. Transcribe a local file (real AssemblyAI SDK -> stub server)"
echo "=================================================================="
OUT_BASE="$TRANSCRIPTS_DIR/week4"
bash "$REPO/transcribe/transcribe.sh" "$LECTURE" "week4" "th" \
     --out-base "$OUT_BASE" > "$TESTROOT/transcribe.log" 2>&1
check "transcribe.sh exits 0" "$?" "0"
[ -s "${OUT_BASE}.txt" ] && ok "transcript .txt written" || bad "no .txt"
[ -s "${OUT_BASE}.srt" ] && ok "subtitle .srt written" || bad "no .srt"
grep -q "Dijkstra" "${OUT_BASE}.txt" && ok "transcript carries the stub text" \
  || bad "transcript text missing"
check "sentence granularity (2 segments, not one per word)" \
  "$(wc -l < "${OUT_BASE}.txt")" "2"
grep -q -- "-->" "${OUT_BASE}.srt" && ok ".srt has real cue timings" \
  || bad ".srt has no timings"
grep -q '"language_code": "th"' "$RECORD_FILE" \
  && ok "language reached the API as th" || bad "language not sent"

echo "--- the key ring advanced its on-disk cursor"
CURSOR="$MEETING_BOT_ROOT/state/keycursor.json"
[ -f "$CURSOR" ] && ok "cursor file written" || bad "no cursor file"
check "AssemblyAI cursor advanced to key 2" \
  "$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['ASSEMBLYAI_API_KEY'])" \
     "$CURSOR" 2>/dev/null)" "1"

echo ""
echo "=================================================================="
echo "4. Summarize (real llm_client -> stub claude CLI) + PDF"
echo "=================================================================="
: > "$RECORD_FILE"
: > "$CLI_RECORD"
SUMMARY_MD="$SUMMARIES_DIR/week4.md"
SUMMARY_PDF="$PDF_DIR/week4.pdf"
"$PY" "$REPO/summarize/summarize.py" "$LECTURE" "${OUT_BASE}.txt" "$SUMMARY_MD" \
      --frames-manifest "$FRAME_OUT/manifest.json" \
      --pdf-out "$SUMMARY_PDF" \
      --prompt lecture-claude \
      --run-id week4_test > "$TESTROOT/summarize.log" 2>&1
check "summarize.py exits 0" "$?" "0"
[ -s "$SUMMARY_MD" ] && ok "markdown written" || bad "no markdown"

echo "--- how the CLI was actually invoked"
# One JSON line per invocation: the argv llm_client built, the prompt it piped
# in, and the auth vars that survived into the child environment.
ARGV=$("$PY" - "$CLI_RECORD" <<'PYEOF'
import json, sys
print(json.dumps(json.loads(open(sys.argv[1]).readline())["argv"]))
PYEOF
)
PROMPT=$("$PY" - "$CLI_RECORD" <<'PYEOF'
import json, sys
sys.stdout.write(json.loads(open(sys.argv[1]).readline())["prompt"])
PYEOF
)
LEAKED=$("$PY" - "$CLI_RECORD" <<'PYEOF'
import json, sys
env = json.loads(open(sys.argv[1]).readline())["env"]
print(",".join(k for k, v in env.items() if v is not None) or "none")
PYEOF
)

echo "$ARGV" | grep -q '"--effort", "medium"' \
  && ok "--effort carried SUMMARY_EFFORT" || bad "effort not passed"
echo "$ARGV" | grep -q '"--model", "opus"' \
  && ok "--model carried CLAUDE_CLI_MODEL" || bad "wrong model"
echo "$ARGV" | grep -q '"-p"' \
  && ok "print mode (non-interactive)" || bad "-p not passed"
echo "$ARGV" | grep -q '"--output-format", "stream-json"' \
  && ok "stream-json output, so the rate_limit_event (the meter) arrives" \
  || bad "output format not stream-json"
echo "$ARGV" | grep -q '"--verbose"' \
  && ok "--verbose, which print mode requires for stream-json" \
  || bad "--verbose not passed"
echo "$ARGV" | grep -q '"--safe-mode"' \
  && ok "--safe-mode: no CLAUDE.md, hooks or plugins bleed in" \
  || bad "--safe-mode not passed"
echo "$ARGV" | grep -q '"--no-session-persistence"' \
  && ok "sessions not persisted (they would fill the disk)" \
  || bad "--no-session-persistence not passed"
# budget_tokens has no CLI spelling at all now, which is the durable fix for
# the parameter current models reject.
echo "$ARGV$PROMPT" | grep -q 'budget_tokens' \
  && bad "budget_tokens appeared (current models reject it)" \
  || ok "no budget_tokens anywhere in the invocation"

echo "--- frames reach the model as image blocks in one turn, not as files"
# --input-format stream-json lets the user turn carry base64 image blocks,
# so the model sees every offered frame at once and needs no tool. On the
# older Read-tool path each frame the model opened was another turn that
# resent the whole context as cache reads — more than the frames cost.
echo "$ARGV" | grep -q '"--input-format", "stream-json"' \
  && ok "stdin is a stream-json user message" || bad "--input-format stream-json not passed"
echo "$ARGV" | grep -q '"--tools", ""' \
  && ok "no tools offered — nothing to Read" || bad "tools were offered"
echo "$ARGV" | grep -q '"--add-dir"' \
  && bad "--add-dir passed; the model was given file access it has no use for" \
  || ok "no --add-dir"
IMAGES=$("$PY" - "$CLI_RECORD" <<'PYEOF'
import json, sys
images = json.loads(open(sys.argv[1]).readline()).get("images") or []
bad = [i for i in images if i.get("media_type") != "image/jpeg" or i.get("bytes", 0) <= 0]
print(len(images), len(bad))
PYEOF
)
[ "${IMAGES%% *}" -ge 1 ] && [ "${IMAGES##* }" = "0" ] \
  && ok "${IMAGES%% *} JPEG image block(s) rode along on stdin" \
  || bad "image blocks wrong: count/bad = $IMAGES"
echo "$PROMPT" | grep -qE "^\[frame [0-9]+ @ [0-9.]+s \((scene_change|periodic)\)\]" \
  && ok "the manifest names the frames the images carry" || bad "no frame labels in the prompt"
echo "$PROMPT" | grep -qF "$FRAME_OUT/" \
  && bad "filesystem paths in the prompt — the model has no tool to open them" \
  || ok "no filesystem paths in the prompt"

echo "--- frames are cropped and downscaled for the model, not on disk"
# A 1920x1080 keyframe costs ~1,844 tokens every time the model sees it. The
# copy is cropped to the slide (PDF_FRAME_CROP, same as the PDF) and fitted
# to FRAME_MAX_DIMENSION; the saved frame pdf.py crops must not move.
"$PY" - "$FRAME_OUT" "$CLI_RECORD" <<'PYEOF'
import base64, io, json, sys
from pathlib import Path
out = Path(sys.argv[1])
try:
    from PIL import Image
except ImportError:
    sys.exit(0)          # Pillow is optional; the fallback is the original.
copies = sorted(out.glob("llm-768*/*.jpg"))
if not copies:
    sys.exit(1)
for c in copies:
    with Image.open(c) as img:
        if max(img.size) > 768:
            sys.exit(2)
    with Image.open(out / c.name) as img:   # the original, still full size
        if max(img.size) <= 768:
            sys.exit(3)
# And the bytes on stdin are those copies, not the originals.
images = json.loads(open(sys.argv[2]).readline()).get("images") or []
for i in images:
    if i["bytes"] > max(c.stat().st_size for c in copies):
        sys.exit(4)
sys.exit(0)
PYEOF
RC=$?
[ "$RC" -eq 0 ] && ok "copies are <=768px, the originals are untouched, the copies are what was sent" \
  || bad "prepared copies wrong, or the originals were modified (code $RC)"
echo "$PROMPT" | grep -q "Dijkstra" \
  && ok "transcript text included in the prompt" || bad "transcript not sent"

echo "--- the subscription, not a metered API key"
[ "$LEAKED" = "none" ] \
  && ok "ANTHROPIC_* scrubbed from the CLI environment" \
  || bad "these reached the CLI and would redirect billing: $LEAKED"

echo "--- the usage ledger: what the stage spent, and the meter"
# The stub answers every call with the same fixed usage and a 42% five-hour
# meter; llm_client adds them up and summarize.py writes the total to the
# run's state.json when run_one.sh launched it (MEETING_BOT_RUN_DIR).
USAGE_RUN="$MEETING_BOT_ROOT/runs/usage_test"
mkdir -p "$USAGE_RUN"
"$PY" "$REPO/lib/runstate.py" init --run-dir "$USAGE_RUN" --input x --input-type local_file --safe-name usage_test
USAGE_MD="$SUMMARIES_DIR/usage.md"
MEETING_BOT_RUN_DIR="$USAGE_RUN" \
  "$PY" "$REPO/summarize/summarize.py" "$LECTURE" "${OUT_BASE}.txt" "$USAGE_MD" \
    --frames-manifest "$FRAME_OUT/manifest.json" --no-pdf \
    --run-id usage_test > "$TESTROOT/usage.log" 2>&1
check "summarize.py exits 0" "$?" "0"
grep -q "Claude usage this stage: 1 call(s)" "$TESTROOT/usage.log" \
  && ok "the stage reports its usage" || bad "no usage line in the log"
grep -q "5h window at 42%" "$TESTROOT/usage.log" \
  && ok "the subscription meter is reported" || bad "meter not reported"
USAGE_JSON=$("$PY" "$REPO/lib/runstate.py" get --run-dir "$USAGE_RUN" --key stages.summarize.usage 2>/dev/null)
echo "$USAGE_JSON" | "$PY" -c '
import json, sys
u = json.load(sys.stdin)
assert u["calls"] == 1, u
assert u["input_tokens"] == 1000 and u["cache_read_input_tokens"] == 4000, u
assert u["thinking_tokens"] == 120, u
assert abs(u["cost_usd"] - 0.25) < 1e-6, u
assert u["windows"]["five_hour"]["utilization_after"] == 0.42, u
' && ok "usage recorded in state.json with the meter" \
  || bad "state.json usage wrong or missing: $USAGE_JSON"

echo "--- an exhausted usage window waits for the reset, then retries"
# The stub refuses the first call with a rejected rate_limit_event whose
# reset is 2s away, and answers the second. The real llm_client must sleep
# through the reset and try again on the subscription — not retry on the
# backoff schedule, and not hand the summary to Gemini.
WAIT_RUN="$MEETING_BOT_ROOT/runs/wait_test"
mkdir -p "$WAIT_RUN"
"$PY" "$REPO/lib/runstate.py" init --run-dir "$WAIT_RUN" --input y --input-type local_file --safe-name wait_test
WAIT_MD="$SUMMARIES_DIR/wait.md"
WAIT_RECORD="$TESTROOT/claude_cli_wait.jsonl"
rm -f "$TESTROOT/wait.calls"
MEETING_BOT_RUN_DIR="$WAIT_RUN" FAKE_CLAUDE_MODE=rate-limited-once \
  FAKE_CLAUDE_STATE="$TESTROOT/wait.calls" FAKE_CLAUDE_RECORD="$WAIT_RECORD" \
  SUMMARY_FALLBACK_CHAIN=claude-cli,gemini GEMINI_API_KEY_1=would-be-wrong \
  "$PY" "$REPO/summarize/summarize.py" "$LECTURE" "${OUT_BASE}.txt" "$WAIT_MD" \
    --frames-manifest "$FRAME_OUT/manifest.json" --no-pdf --prompt lecture-claude \
    --run-id wait_test > "$TESTROOT/wait.log" 2>&1
check "summarize.py exits 0 after the wait" "$?" "0"
check "the CLI was called twice (refused, then answered)" \
  "$(wc -l < "$WAIT_RECORD")" "2"
grep -q "usage window exhausted" "$TESTROOT/wait.log" \
  && ok "the wait is announced with the window and reset" || bad "no wait line"
grep -q "window should have reset" "$TESTROOT/wait.log" \
  && ok "the retry follows the wait" || bad "no retry after the wait"
grep -q "trying gemini" "$TESTROOT/wait.log" \
  && bad "the chain advanced to Gemini on a hit window" \
  || ok "Gemini was not tried"
grep -q "retrying in" "$TESTROOT/wait.log" \
  && bad "the hit window went through the backoff schedule" \
  || ok "not retried on the backoff schedule"
grep -q "claude-cli/opus" "$WAIT_MD" \
  && ok "the document is Claude's" || bad "provenance is not claude-cli"
"$PY" "$REPO/lib/runstate.py" get --run-dir "$WAIT_RUN" --key stages.summarize.waiting_until >/dev/null 2>&1 \
  && bad "waiting_until left behind after the wait ended" \
  || ok "waiting_until cleared once the call went through"

echo "--- past the wait cap, the stage pauses with the reset time recorded"
# Same refusal, but the wait cap is 0: the stage must exit 75, leave the
# reset time in state.json for --resume-all, and still not try Gemini.
PAUSE_RUN="$MEETING_BOT_ROOT/runs/pause_test"
mkdir -p "$PAUSE_RUN"
"$PY" "$REPO/lib/runstate.py" init --run-dir "$PAUSE_RUN" --input z --input-type local_file --safe-name pause_test
PAUSE_MD="$SUMMARIES_DIR/pause.md"
MEETING_BOT_RUN_DIR="$PAUSE_RUN" FAKE_CLAUDE_MODE=rate-limited FAKE_CLAUDE_RESET_IN=7200 \
  CLAUDE_CLI_MAX_WAIT_SECONDS=0 SUMMARY_FALLBACK_CHAIN=claude-cli,gemini GEMINI_API_KEY_1=would-be-wrong \
  "$PY" "$REPO/summarize/summarize.py" "$LECTURE" "${OUT_BASE}.txt" "$PAUSE_MD" \
    --frames-manifest "$FRAME_OUT/manifest.json" --no-pdf \
    --run-id pause_test > "$TESTROOT/pause.log" 2>&1
check "summarize.py exits 75 (EX_TEMPFAIL) on a pause" "$?" "75"
[ -f "$PAUSE_MD" ] && bad "a document was written for a paused run" \
  || ok "no document written"
PAUSE_AT=$("$PY" "$REPO/lib/runstate.py" get --run-dir "$PAUSE_RUN" --key stages.summarize.rate_limited.resets_at 2>/dev/null)
[ -n "$PAUSE_AT" ] && [ "$PAUSE_AT" -gt "$(date +%s)" ] \
  && ok "the reset time is in state.json for --resume-all" \
  || bad "no usable reset time recorded: '$PAUSE_AT'"
grep -q "trying gemini" "$TESTROOT/pause.log" \
  && bad "the chain advanced to Gemini on a pause" || ok "Gemini not tried on a pause"
grep -q "PAUSED" "$TESTROOT/pause.log" && ok "the log says PAUSED" || bad "no PAUSED line"
"$PY" "$REPO/lib/runstate.py" get --run-dir "$PAUSE_RUN" --key stages.summarize.usage.calls >/dev/null 2>&1 \
  && ok "the refused call is still on the usage ledger" \
  || bad "usage not recorded for the refused call"

echo "--- a signed-out CLI degrades instead of failing the run"
# The CLI exits 0 when it is not logged in, so the only signal is the body.
# This must read as BackendUnavailable and advance the chain, not as a
# transient error worth five retries.
NOAUTH_MD="$SUMMARIES_DIR/noauth.md"
FAKE_CLAUDE_MODE=not-logged-in SUMMARY_BACKEND=fallback \
  SUMMARY_FALLBACK_CHAIN=claude-cli GEMINI_API_KEY_1= \
  "$PY" "$REPO/summarize/summarize.py" "$LECTURE" "${OUT_BASE}.txt" "$NOAUTH_MD" \
    --frames-manifest "$FRAME_OUT/manifest.json" --no-pdf \
    --run-id noauth_test > "$TESTROOT/noauth.log" 2>&1
NOAUTH_RC=$?
[ "$NOAUTH_RC" -ne 0 ] && ok "a signed-out CLI fails the stage (rc=$NOAUTH_RC)" \
  || bad "a signed-out CLI was treated as success"
grep -qi "not logged in" "$TESTROOT/noauth.log" \
  && ok "the error names the real cause" || bad "cause not reported"
grep -qi "unavailable" "$TESTROOT/noauth.log" \
  && ok "classified as BackendUnavailable, so the chain advances" \
  || bad "not classified as unavailable — the chain would retry pointlessly"

echo "--- the unchanging instructions go in as a cacheable system prompt"
# Claude caches an exact prefix. summarize-v2.md fences the half that never
# varies; llm_client passes it as --append-system-prompt-file so the prefix is
# byte-identical across runs and across the chunks of one run. Anything that
# varies leaking into that file makes the cache silently never hit.
V2_MD="$SUMMARIES_DIR/v2.md"
V2_RECORD="$TESTROOT/claude_cli_v2.jsonl"
FAKE_CLAUDE_RECORD="$V2_RECORD" \
  "$PY" "$REPO/summarize/summarize.py" "$LECTURE" "${OUT_BASE}.txt" "$V2_MD" \
    --frames-manifest "$FRAME_OUT/manifest.json" --no-pdf \
    --prompt summarize-v2 --run-id v2_test > "$TESTROOT/v2.log" 2>&1
check "summarize.py --prompt summarize-v2 exits 0" "$?" "0"

V2_ARGV=$("$PY" - "$V2_RECORD" <<'PYEOF'
import json, sys
print(json.dumps(json.loads(open(sys.argv[1]).readline())["argv"]))
PYEOF
)
V2_PROMPT=$("$PY" - "$V2_RECORD" <<'PYEOF'
import json, sys
sys.stdout.write(json.loads(open(sys.argv[1]).readline())["prompt"])
PYEOF
)
echo "$V2_ARGV" | grep -q '"--append-system-prompt-file"' \
  && ok "the static half is passed as a system prompt file" \
  || bad "--append-system-prompt-file not passed"
echo "$V2_ARGV" | grep -q '"--exclude-dynamic-system-prompt-sections"' \
  && ok "the CLI's own per-machine sections move out of the system prompt" \
  || bad "--exclude-dynamic-system-prompt-sections not passed"

SYSFILE=$("$PY" - "$V2_RECORD" <<'PYEOF'
import json, sys
argv = json.loads(open(sys.argv[1]).readline())["argv"]
print(argv[argv.index("--append-system-prompt-file") + 1]
      if "--append-system-prompt-file" in argv else "")
PYEOF
)
[ -s "$SYSFILE" ] && ok "the system prompt file exists on disk" \
  || bad "system prompt file missing: $SYSFILE"
grep -q "output_format" "$SYSFILE" \
  && ok "it carries the output format" || bad "no output format in it"
grep -q "Dijkstra" "$SYSFILE" \
  && bad "the transcript leaked into the cacheable prefix" \
  || ok "no transcript in the cacheable prefix"
grep -qF "$FRAME_OUT" "$SYSFILE" \
  && bad "per-run frame paths leaked into the cacheable prefix" \
  || ok "no per-run paths in the cacheable prefix"
grep -q "static-prompt:" "$SYSFILE" \
  && bad "the delimiter markers reached the model" \
  || ok "the delimiter markers are stripped"
echo "$V2_PROMPT" | grep -q "Dijkstra" \
  && ok "the transcript is in the piped user turn, where it belongs" \
  || bad "transcript missing from the user turn"
echo "$V2_PROMPT" | grep -q "output_format>" \
  && echo "$V2_PROMPT" | grep -q "Bullet list of concrete decisions" \
  && bad "the instructions were sent twice" \
  || ok "the instructions are not duplicated in the user turn"

echo "--- the document wrapper"
grep -q "meeting-transcriber" "$SUMMARY_MD" && ok "provenance comment present" \
  || bad "no provenance comment"
grep -q "claude-cli/opus" "$SUMMARY_MD" \
  && ok "provenance names the backend that answered" || bad "backend not recorded"
grep -q "View Transcript" "$SUMMARY_MD" && ok "transcript embedded in <details>" \
  || bad "transcript block missing"
grep -q "Chapter N" "$SUMMARY_MD" && ok "chapter placeholder present" \
  || bad "no chapter line"

if [ "$HAVE_PDF" -eq 1 ]; then
  [ -s "$SUMMARY_PDF" ] && ok "PDF written" || bad "no PDF"
  head -c 4 "$SUMMARY_PDF" | grep -q "%PDF" && ok "PDF has a PDF header" \
    || bad "PDF is not a PDF"
  PDF_BYTES=$(stat -c %s "$SUMMARY_PDF" 2>/dev/null || echo 0)
  [ "$PDF_BYTES" -gt 8000 ] \
    && ok "PDF is $PDF_BYTES bytes — frames were embedded" \
    || bad "PDF is only $PDF_BYTES bytes; frames probably missing"
  # render() deletes the crop directory it invents, so nothing should be left
  # beside the deliverable — on a synced PDF_DIR that scratch dir was pure
  # noise re-uploaded on every run.
  LEFTOVER=$(find "$PDF_DIR" -name '.pdf-frames' -o -name 'frame_*.jpg' | wc -l)
  [ "$LEFTOVER" -eq 0 ] && ok "no crop scratch directory left in PDF_DIR" \
    || bad "$LEFTOVER leftover crop file(s)/dir(s) in PDF_DIR"
  # A caller that names its own work_dir still owns it — that is how the
  # cropped copies stay inspectable, so cropping is still asserted directly.
  CROPDIR="$TESTROOT/crops"
  "$PY" "$REPO/summarize/pdf.py" "$SUMMARY_MD" "$TESTROOT/crop_probe.pdf" \
        --frames-manifest "$FRAME_OUT/manifest.json" \
        --work-dir "$CROPDIR" > "$TESTROOT/crop_probe.log" 2>&1
  CROPPED=$(find "$CROPDIR" -name 'frame_*.jpg' 2>/dev/null | wc -l)
  [ "$CROPPED" -ge 1 ] && ok "$CROPPED frame(s) cropped for the PDF" \
    || bad "no cropped frames were produced"
  [ -d "$CROPDIR" ] && ok "an explicit --work-dir is left for the caller" \
    || bad "an explicit --work-dir was deleted; the caller owns it"
  "$PY" - "$FRAME_OUT" <<'PYEOF' && ok "slide region detected in a real frame" \
    || bad "crop declined on every frame (slide detection regressed)"
import sys, glob
sys.path.insert(0, __import__("os").environ["REPO_DIR"] + "/summarize")
import framecrop
frames = sorted(glob.glob(sys.argv[1] + "/*.jpg"))
sys.exit(0 if any(framecrop.detect_crop(f, "slide") for f in frames) else 1)
PYEOF
else
  echo "  skip — weasyprint/markdown/Pillow not installed; PDF assertions skipped"
fi

echo ""
echo "=================================================================="
echo "4b. Several videos summarized as one (summarize.py --parts)"
echo "=================================================================="
# The same lecture twice stands in for two videos. That is deliberate: the
# two share every timestamp, so if the labels the model is shown did not name
# the video, nothing here could tell frame 1 of video 1 from frame 1 of
# video 2 — which is exactly the ambiguity the per-video clock has to remove.
PARTS_JSON="$TESTROOT/parts.json"
PARTS_MD="$SUMMARIES_DIR/chapter.md"
PARTS_PDF="$PDF_DIR/chapter.pdf"
PARTS_RECORD="$TESTROOT/claude_cli_parts.jsonl"
cat > "$PARTS_JSON" <<EOF
{"parts": [
  {"source": "$LECTURE", "kind": "local_file", "title": "Week 4 A",
   "transcript": "${OUT_BASE}.txt", "srt": "${OUT_BASE}.srt",
   "frames_manifest": "$FRAME_OUT/manifest.json"},
  {"source": "$LECTURE", "kind": "local_file", "title": "Week 4 B",
   "clip": "00:00:30-end",
   "transcript": "${OUT_BASE}.txt", "srt": "${OUT_BASE}.srt",
   "frames_manifest": "$FRAME_OUT/manifest.json"}
]}
EOF
FAKE_CLAUDE_RECORD="$PARTS_RECORD" \
  "$PY" "$REPO/summarize/summarize.py" --parts "$PARTS_JSON" "$PARTS_MD" \
      --pdf-out "$PARTS_PDF" --prompt lecture-claude \
      --run-id combine_test > "$TESTROOT/parts.log" 2>&1
check "summarize.py --parts exits 0" "$?" "0"
[ -s "$PARTS_MD" ] && ok "combined markdown written" || bad "no combined markdown"
check "one summarize call for the whole set (fits under the chunk limit)" \
  "$(wc -l < "$PARTS_RECORD")" "1"
PARTS_PROMPT=$("$PY" - "$PARTS_RECORD" <<'PYEOF'
import json, sys
sys.stdout.write(json.loads(open(sys.argv[1]).readline())["prompt"])
PYEOF
)
echo "$PARTS_PROMPT" | grep -q "=== video 1 of 2: Week 4 A ===" \
  && ok "transcript fenced with video 1's label" || bad "no video 1 fence"
echo "$PARTS_PROMPT" | grep -q "=== video 2 of 2: Week 4 B (clip 00:00:30-end) ===" \
  && ok "video 2's fence carries its clip window" || bad "no video 2 fence"
check "the transcript appears once per video" \
  "$(echo "$PARTS_PROMPT" | grep -c "Dijkstra")" "2"
echo "$PARTS_PROMPT" | grep -qE "\[frame 1 @ video 1 [0-9.]+s" \
  && ok "video 1's frames are labelled with the video" || bad "frame label lacks video 1"
NFRAMES=$("$PY" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["frames"]))' "$FRAME_OUT/manifest.json")
echo "$PARTS_PROMPT" | grep -qE "\[frame $((NFRAMES + 1)) @ video 2 " \
  && ok "video 2's numbering continues after video 1's ($NFRAMES frames)" \
  || bad "video 2 does not continue the numbering"
echo "$PARTS_PROMPT" | grep -qE "\[frame 1 @ video 2 " \
  && bad "video 2 restarted the frame numbers at 1" \
  || ok "no duplicate frame numbers across videos"
echo "--- the combined document wrapper"
grep -q "source_type: combined" "$PARTS_MD" && ok "provenance says combined" \
  || bad "provenance does not say combined"
grep -q "^# Week 4 A" "$PARTS_MD" && ok "one title, the first video's" \
  || bad "title missing"
grep -q "Source File (Video 1):" "$PARTS_MD" \
  && grep -q "Source File (Video 2):" "$PARTS_MD" \
  && ok "one link line per video" || bad "link lines missing"
grep -q 'Clip (Video 2): `00:00:30-end`' "$PARTS_MD" \
  && ok "video 2's clip window is stated" || bad "clip line missing"
grep -q "Summarized from 2 videos as one" "$PARTS_MD" \
  && ok "the document says timestamps are per video" || bad "no per-video note"
check "exactly one transcript block" "$(grep -c '<details>' "$PARTS_MD")" "1"
grep -q "=== video 2 of 2" "$PARTS_MD" \
  && ok "the embedded transcript keeps the video fences" || bad "fences lost"
if [ "$HAVE_PDF" -eq 1 ]; then
  [ -s "$PARTS_PDF" ] && ok "combined PDF written" || bad "no combined PDF"
  # Re-rendering from the .md needs one manifest per video, in order.
  "$PY" "$REPO/summarize/pdf.py" "$PARTS_MD" "$TESTROOT/parts_rerender.pdf" \
        --frames-manifest "$FRAME_OUT/manifest.json" \
        --frames-manifest "$FRAME_OUT/manifest.json" > "$TESTROOT/parts_rerender.log" 2>&1
  check "pdf.py re-renders a combined document from two manifests" "$?" "0"
fi

echo ""
echo "=================================================================="
echo "5. Reference material (--resources)"
echo "=================================================================="
mkdir -p "$TESTROOT/course notes"
cat > "$TESTROOT/course notes/week4.md" <<'EOF'
# Week 4 — Shortest paths
The correct spelling is Bellman-Ford, and the bound is O(VE).
EOF
: > "$CLI_RECORD"
"$PY" "$REPO/summarize/summarize.py" "$LECTURE" "${OUT_BASE}.txt" \
      "$SUMMARIES_DIR/week4_res.md" \
      --frames-manifest "$FRAME_OUT/manifest.json" \
      --pdf-out "$PDF_DIR/week4_res.pdf" \
      --prompt lecture-claude \
      --resources "$TESTROOT/course notes" > "$TESTROOT/resources.log" 2>&1
check "summarize with --resources exits 0" "$?" "0"
# The prompt is what the CLI is handed on stdin, so that is where the slides
# have to show up now — not in an HTTP request body.
grep -q "Bellman-Ford" "$CLI_RECORD" \
  && ok "reference material reached the model" || bad "resources not sent"
grep -q "Reference material" "$CLI_RECORD" \
  && ok "material is framed as reference data" || bad "no reference framing"

echo "--- a missing local resource path is a typo, and fails fast"
"$PY" "$REPO/summarize/summarize.py" "$LECTURE" "${OUT_BASE}.txt" \
      "$SUMMARIES_DIR/nope.md" \
      --frames-manifest "$FRAME_OUT/manifest.json" \
      --resources "$TESTROOT/definitely-not-here" \
      > "$TESTROOT/badres.log" 2>&1
rc=$?
check "bad --resources exits non-zero" "$([ "$rc" -ne 0 ] && echo yes || echo no)" "yes"
grep -q "does not exist" "$TESTROOT/badres.log" \
  && ok "says which path is missing" || bad "unclear error"

echo ""
echo "=================================================================="
echo "6. YouTube captions (real client -> stub server)"
echo "=================================================================="
: > "$RECORD_FILE"
YT_BASE="$TRANSCRIPTS_DIR/yt_stub"
bash "$REPO/transcribe/transcribe.sh" \
     "https://www.youtube.com/watch?v=stubvideo01" "yt_stub" "en" \
     --out-base "$YT_BASE" > "$TESTROOT/yt.log" 2>&1
check "transcribe.sh (YouTube) exits 0" "$?" "0"
check "two timed segments, not one flat blob" "$(wc -l < "${YT_BASE}.txt")" "2"
grep -q "Welcome to the lecture" "${YT_BASE}.txt" \
  && ok "caption markup unescaped and stripped" || bad "markup not cleaned"
grep -q "Dijkstra & Bellman-Ford" "${YT_BASE}.txt" \
  && ok "HTML entities decoded" || bad "entities not decoded"
grep -q "00:00:04,000" "${YT_BASE}.srt" \
  && ok "caption timings preserved in the .srt" || bad "timings lost"
grep -q "whole transcript in one useless string" "${YT_BASE}.txt" \
  && bad "fell back to the untimed flat text field" \
  || ok "used tracks[].transcript, not the flat text field"
check "YouTube key cursor advanced" \
  "$("$PY" -c "import json,sys;print(json.load(open(sys.argv[1]))['YT_TRANSCRIPT_KEY'])" \
     "$CURSOR" 2>/dev/null)" "1"

echo ""
echo "=================================================================="
echo "7. The whole pipeline over a local file"
echo "=================================================================="
out=$( cd "$REPO" && bash ./pipeline.sh "$LECTURE" --prompt lecture-claude \
       --resources "$TESTROOT/course notes" 2>&1 )
rc=$?
echo "$out" > "$TESTROOT/pipeline.log"
check "pipeline.sh exits 0" "$rc" "0"
RUN=$("$PY" "$REPO/lib/runstate.py" latest --root "$MEETING_BOT_ROOT/runs")
for stage in transcribe frames summarize; do
  check "pipeline: $stage done" \
    "$("$PY" "$REPO/lib/runstate.py" status --run-dir "$MEETING_BOT_ROOT/runs/$RUN" --stage $stage)" \
    "done"
done
[ -s "$SUMMARIES_DIR/$RUN.md" ] && ok "summary written to SUMMARIES_DIR" \
  || bad "no summary in SUMMARIES_DIR"
if [ "$HAVE_PDF" -eq 1 ]; then
  [ -s "$PDF_DIR/$RUN.pdf" ] && ok "PDF written to PDF_DIR" || bad "no PDF in PDF_DIR"
fi

echo ""
echo "=================================================================="
echo "Result: $PASS passed, $FAIL failed"
echo "=================================================================="
[ "$FAIL" -eq 0 ] || exit 1
