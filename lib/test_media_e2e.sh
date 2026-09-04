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
trap cleanup EXIT

# --- Preconditions -----------------------------------------------------------
missing=""
command -v ffmpeg >/dev/null 2>&1 || missing="$missing ffmpeg"
"$PY" -c "import assemblyai" 2>/dev/null || missing="$missing assemblyai"
"$PY" -c "import anthropic" 2>/dev/null || missing="$missing anthropic"
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
export ANTHROPIC_API_KEY="stub-anthropic-key"
export ASSEMBLYAI_API_KEY_1="stub-aai-1"
export ASSEMBLYAI_API_KEY_2="stub-aai-2"
export ASSEMBLYAI_API_KEY_3="stub-aai-3"
export YT_TRANSCRIPT_KEY_1="stub-yt-1"
export YT_TRANSCRIPT_KEY_2="stub-yt-2"
export SUMMARY_BACKEND=anthropic
export SUMMARY_EFFORT=medium
export ANTHROPIC_MODEL=claude-opus-5
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

start_stub anthropic 8801 || exit 1
start_stub assemblyai 8802 || exit 1
start_stub youtube 8803 || exit 1
export ANTHROPIC_BASE_URL="http://127.0.0.1:8801"
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
echo "4. Summarize (real anthropic SDK -> stub server) + PDF"
echo "=================================================================="
: > "$RECORD_FILE"
SUMMARY_MD="$SUMMARIES_DIR/week4.md"
SUMMARY_PDF="$PDF_DIR/week4.pdf"
"$PY" "$REPO/summarize/summarize.py" "$LECTURE" "${OUT_BASE}.txt" "$SUMMARY_MD" \
      --frames-manifest "$FRAME_OUT/manifest.json" \
      --pdf-out "$SUMMARY_PDF" \
      --prompt lecture-claude \
      --run-id week4_test > "$TESTROOT/summarize.log" 2>&1
check "summarize.py exits 0" "$?" "0"
[ -s "$SUMMARY_MD" ] && ok "markdown written" || bad "no markdown"

echo "--- what actually went over the wire"
REQ=$("$PY" - "$RECORD_FILE" <<'PYEOF'
import json, sys
for line in open(sys.argv[1]):
    entry = json.loads(line)
    if entry["api"] == "anthropic":
        print(json.dumps(entry["body"]))
        break
PYEOF
)
echo "$REQ" | grep -q '"effort": "medium"' \
  && ok "output_config.effort carried SUMMARY_EFFORT" || bad "effort not sent"
echo "$REQ" | grep -q '"type": "adaptive"' \
  && ok "adaptive thinking requested" || bad "thinking not sent"
echo "$REQ" | grep -q '"budget_tokens"' \
  && bad "budget_tokens was sent (current models reject it)" \
  || ok "no budget_tokens (rejected by current models)"
echo "$REQ" | grep -q '"model": "claude-opus-5"' \
  && ok "model is claude-opus-5" || bad "wrong model"
echo "$REQ" | grep -q '"type": "image"' \
  && ok "frames attached as image blocks" || bad "no images attached"
echo "$REQ" | grep -q "Dijkstra" \
  && ok "transcript text included in the prompt" || bad "transcript not sent"

echo "--- the document wrapper"
grep -q "meeting-transcriber" "$SUMMARY_MD" && ok "provenance comment present" \
  || bad "no provenance comment"
grep -q "anthropic/claude-opus-5" "$SUMMARY_MD" \
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
  # The cropped copies are what get embedded; confirm cropping actually ran.
  CROPPED=$(find "$PDF_DIR" -name 'frame_*.jpg' | wc -l)
  [ "$CROPPED" -ge 1 ] && ok "$CROPPED frame(s) cropped for the PDF" \
    || bad "no cropped frames were produced"
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
echo "5. Reference material (--resources)"
echo "=================================================================="
mkdir -p "$TESTROOT/course notes"
cat > "$TESTROOT/course notes/week4.md" <<'EOF'
# Week 4 — Shortest paths
The correct spelling is Bellman-Ford, and the bound is O(VE).
EOF
: > "$RECORD_FILE"
"$PY" "$REPO/summarize/summarize.py" "$LECTURE" "${OUT_BASE}.txt" \
      "$SUMMARIES_DIR/week4_res.md" \
      --frames-manifest "$FRAME_OUT/manifest.json" \
      --pdf-out "$PDF_DIR/week4_res.pdf" \
      --prompt lecture-claude \
      --resources "$TESTROOT/course notes" > "$TESTROOT/resources.log" 2>&1
check "summarize with --resources exits 0" "$?" "0"
grep -q "Bellman-Ford" "$RECORD_FILE" \
  && ok "reference material reached the model" || bad "resources not sent"
grep -q "Reference material" "$RECORD_FILE" \
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
