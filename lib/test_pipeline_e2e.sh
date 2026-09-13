#!/bin/bash
# End-to-end test of the orchestration logic, with the four expensive stages
# stubbed out.
#
# What's REAL here: pipeline.sh and run_one.sh in full — input classification,
# run-id derivation, the stage DAG, the parallel branches, state.json
# transitions, auto-resume, --force, --jobs, --combine, and the failure paths.
#
# What's STUBBED: record_screen.sh (needs a live meeting + Chrome), transcribe.sh
# (needs an AssemblyAI key), extract_frames.py (needs a real video), and
# summarize.py (needs an LLM key). Each stub honors the same argument and
# output-file contract as the real thing, so the orchestration around them runs
# for real. Stubs can be told to fail via a sentinel file, which is how the
# resume tests are driven.
#
# This does NOT prove Chrome can join a Meet call or that your API keys work.
# It proves the machinery between those things is correct.
#
#     ./lib/test_pipeline_e2e.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# A path with a space in it, on purpose: the five output directories are
# operator-configured and one of them WILL eventually live under
# "/mnt/Course Material". Every quoting mistake in the pipeline shows up here.
TESTROOT="$(mktemp -d)/test root"
mkdir -p "$TESTROOT"
export MEETING_BOT_ROOT="$TESTROOT/opt"
# The five media directories are independent env vars now, with no defaults —
# an unset one is an error, which section 8 checks.
export RECORDINGS_DIR="$TESTROOT/opt/recordings"
export TRANSCRIPTS_DIR="$TESTROOT/opt/transcripts"
export FRAMES_DIR="$TESTROOT/opt/frames"
export SUMMARIES_DIR="$TESTROOT/opt/summaries"
export PDF_DIR="$TESTROOT/opt/pdf"
FAKE_BIN="$TESTROOT/bin"
STAGING="$TESTROOT/repo"
PASS=0
FAIL=0

cleanup() { rm -rf "$(dirname "$TESTROOT")"; }
trap cleanup EXIT

ok()   { PASS=$((PASS + 1)); echo "  ok   — $1"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL — $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$3', got '$2')"; fi; }

# --- Build a staging copy of the repo with the four stages stubbed -----------
mkdir -p "$STAGING" "$FAKE_BIN"
cp -r "$REPO"/{pipeline.sh,source_env.sh,lib,summarize,transcribe,screen} "$STAGING/"
# No .env in the test tree: source_env.sh must not pick up the developer's real
# keys and start making live API calls from a test run.
: > "$STAGING/.env"

cat > "$STAGING/screen/record_screen.sh" <<'STUB'
#!/bin/bash
# stub recorder: writes an "MP4" at the path the orchestrator chose
[ -f "$STUB_FAIL_RECORD" ] && { echo "stub: record failing on purpose" >&2; exit 1; }
mkdir -p "$(dirname "$4")"; echo "fake mp4 for $1" > "$4"
echo "stub: recorded $4"
STUB

cat > "$STAGING/transcribe/transcribe.sh" <<'STUB'
#!/bin/bash
# stub transcriber: honors --out-base, writes .txt + .srt
[ -f "$STUB_FAIL_TRANSCRIBE" ] && { echo "stub: transcribe failing on purpose" >&2; exit 1; }
# The nastier failure: exit 0 having written nothing. transcribe.sh really
# did this (it captured $? inside `if ! cmd; then`, where $? is the negated
# condition and so always 0), and run_one.sh recorded artifacts that were
# never created.
[ -f "$STUB_LIE_TRANSCRIBE" ] && { echo "stub: transcribe claiming success without output"; exit 0; }
# Record what the orchestrator handed us, before the loop below shifts it
# away, so a test can assert on the routing (--media on the Kaltura path, the
# input itself on the YouTube one).
[ -n "${STUB_TRANSCRIBE_ARGS:-}" ] && printf '%s\n' "$@" > "$STUB_TRANSCRIBE_ARGS"
out=""
while [ "$#" -gt 0 ]; do
  case "$1" in --out-base) out="$2"; shift 2 ;; *) args+=("$1"); shift ;; esac
done
[ -n "$out" ] || out="/tmp/stub_transcript"
mkdir -p "$(dirname "$out")"
# Trace start/end so a test can tell whether two sessions overlapped.
if [ -n "${STUB_TRACE:-}" ]; then
  echo "start $(basename "$out")" >> "$STUB_TRACE"
  sleep "${STUB_TRANSCRIBE_SECONDS:-0}"
  echo "end $(basename "$out")" >> "$STUB_TRACE"
fi
# Long enough to trigger the chunking path in a real summarize run.
for i in $(seq 1 50); do echo "transcript line $i for ${args[0]}"; done > "$out.txt"
printf '1\n00:00:00,000 --> 00:00:05,000\nline one\n' > "$out.srt"
echo "stub: transcribed -> $out.txt"
STUB

cat > "$STAGING/screen/extract_frames.py" <<'STUB'
#!/usr/bin/env python3
import json, os, sys
if os.path.exists(os.environ.get("STUB_FAIL_FRAMES", "/nonexistent")):
    sys.stderr.write("stub: frames failing on purpose\n"); sys.exit(1)
video, out_dir = sys.argv[1], sys.argv[2]
os.makedirs(out_dir, exist_ok=True)
json.dump({"video": video, "frame_count": 2, "frames": [
    {"timestamp_s": 1.0, "kind": "scene_change", "path": f"{out_dir}/a.jpg"},
    {"timestamp_s": 9.0, "kind": "periodic", "path": f"{out_dir}/b.jpg"}]},
    open(f"{out_dir}/manifest.json", "w"))
print(f"stub: frames -> {out_dir}/manifest.json")
STUB

cat > "$STAGING/summarize/summarize.py" <<'STUB'
#!/usr/bin/env python3
import os, sys
if os.path.exists(os.environ.get("STUB_FAIL_SUMMARIZE", "/nonexistent")):
    sys.stderr.write("stub: summarize failing on purpose\n"); sys.exit(1)
if os.path.exists(os.environ.get("STUB_PAUSE_SUMMARIZE", "/nonexistent")):
    # What the real summarize.py does when the Claude usage window is
    # exhausted and the wait gave up: record the reset time on the stage
    # (the file holds the unix time) and exit 75.
    import subprocess
    resets_at = open(os.environ["STUB_PAUSE_SUMMARIZE"]).read().strip() or "0"
    subprocess.run([sys.executable, os.environ["STUB_RUNSTATE"], "annotate",
                    "--run-dir", os.environ["MEETING_BOT_RUN_DIR"], "--stage", "summarize",
                    "--set", 'rate_limited={"window": "five_hour", "resets_at": %s, '
                             '"resets_at_iso": "later"}' % resets_at], check=True)
    sys.stderr.write("stub: PAUSED on the usage window\n"); sys.exit(75)
argv = sys.argv[1:]
flags = {}
args = []
i = 0
while i < len(argv):
    if argv[i].startswith("--") and i + 1 < len(argv) and not argv[i + 1].startswith("--"):
        flags.setdefault(argv[i], []).append(argv[i + 1]); i += 2
    elif argv[i].startswith("--"):
        flags.setdefault(argv[i], []); i += 1
    else:
        args.append(argv[i]); i += 1
# Record what we were handed so the tests can assert on it.
with open(os.environ["STUB_SUMMARIZE_ARGS"], "w") as fh:
    fh.write("\n".join(argv))
if "--parts" in flags:
    # The --combine path: one call over every member's transcript + frames.
    # The parts file is what the tests inspect; the "summary" it writes
    # names each part so the document proves it saw all of them.
    import json
    parts = json.load(open(flags["--parts"][0]))["parts"]
    for i, part in enumerate(parts, start=1):
        assert os.path.isfile(part["transcript"]), f"part {i}: no transcript"
        assert os.path.isfile(part["frames_manifest"]), f"part {i}: no manifest"
    out = args[0]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    body = "\n".join(f"video {i}: {p['source']}" for i, p in enumerate(parts, start=1))
    open(out, "w").write(f"<!-- meeting-transcriber\n     source_type: combined\n-->\n\n"
                         f"# Stub combined\n\n{body}\n\n"
                         f"See (Frame 3 @ video 2 9.0s).\n\n<br><br>\n")
    print(f"stub: combined summary of {len(parts)} parts -> {out}")
    if "--pdf-out" in flags and os.environ.get("STUB_SUMMARIZE_NO_PDF") != "1":
        pdf = flags["--pdf-out"][0]
        os.makedirs(os.path.dirname(pdf), exist_ok=True)
        open(pdf, "wb").write(b"%PDF-1.4 stub\n")
        print(f"stub: pdf -> {pdf}")
    sys.exit(0)
# Assert the orchestrator handed us a pre-extracted manifest rather than making
# us re-run frame extraction, and told us where the PDF goes (PDF_DIR is not
# derivable from the .md path — the two directories are configured separately).
assert "--frames-manifest" in flags, "pipeline must pass --frames-manifest"
assert "--pdf-out" in flags, "pipeline must pass --pdf-out"
out = args[2]
os.makedirs(os.path.dirname(out), exist_ok=True)
if os.environ.get("STUB_SUMMARIZE_NO_MARKDOWN") != "1":
    open(out, "w").write(f"<!-- meeting-transcriber\n     source: x\n-->\n\n"
                         f"# Stub\n\nsummary of {args[0]}\n\n"
                         f"See (Frame 1 @ 0:00:01).\n\n<br><br>\n")
    print(f"stub: summary -> {out}")
pdf = flags["--pdf-out"][0]
if os.environ.get("STUB_SUMMARIZE_NO_PDF") != "1":
    os.makedirs(os.path.dirname(pdf), exist_ok=True)
    open(pdf, "wb").write(b"%PDF-1.4 stub\n")
    print(f"stub: pdf -> {pdf}")
STUB

# Kaltura: only the network half is stubbed. `parse` is delegated to the real
# module, because the orchestration under test depends on what it returns —
# the run id (kal_<entry>) and the canonical URL that replaces a pasted
# <iframe> in the summary's source line. lib/test_kaltura.py covers the parser
# itself; this covers the wiring around it.
cp "$STAGING/lib/kaltura.py" "$STAGING/lib/kaltura_real.py"
cat > "$STAGING/lib/kaltura.py" <<'STUB'
#!/usr/bin/env python3
import json, os, subprocess, sys
REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kaltura_real.py")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd == "parse":
    sys.exit(subprocess.call([sys.executable, REAL] + sys.argv[1:]))
if os.path.exists(os.environ.get("STUB_FAIL_KALTURA", "/nonexistent")):
    sys.stderr.write("stub: kaltura failing on purpose\n"); sys.exit(1)
if cmd == "info":
    print(json.dumps({"partner_id": "2910381", "entry_id": "1_y9jay9sw",
                      "title": os.environ.get("STUB_KALTURA_TITLE", "Stub Kaltura Lecture"),
                      "duration": 60, "captions": []}))
    sys.exit(0)
if cmd == "download":
    path = sys.argv[3]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").write("fake kaltura video\n")
    print(path); sys.exit(0)
if cmd == "captions":
    if os.environ.get("STUB_KALTURA_CAPTIONS") == "1":
        json.dump([{"text": "stub caption", "offset_ms": 0, "duration_ms": 1000}],
                  sys.stdout)
        sys.exit(0)
    sys.stderr.write("stub: no usable caption track\n"); sys.exit(3)
sys.stderr.write(f"stub: unknown command {cmd}\n"); sys.exit(1)
STUB
chmod +x "$STAGING/lib/kaltura.py"

chmod +x "$STAGING/screen/record_screen.sh" "$STAGING/transcribe/transcribe.sh" \
         "$STAGING/screen/extract_frames.py" "$STAGING/summarize/summarize.py"

# yt-dlp stub, so the YouTube path doesn't hit the network here.
cat > "$FAKE_BIN/yt-dlp" <<'STUB'
#!/bin/bash
[ -f "$STUB_FAIL_FETCH" ] && { echo "stub: yt-dlp failing on purpose" >&2; exit 1; }
out=""; while [ "$#" -gt 0 ]; do
  case "$1" in -o) out="$2"; shift 2 ;; *) shift ;; esac
done
path="${out/%.%(ext)s/.mp4}"
mkdir -p "$(dirname "$path")"; echo "fake youtube video" > "$path"
echo "stub: downloaded $path"
STUB
chmod +x "$FAKE_BIN/yt-dlp"

# ffmpeg, for the clip stage. lib/clip.py is NOT stubbed — the window parsing,
# the flag order and the .part rename are the parts worth exercising against
# the real orchestrator — so only the encoder underneath it is faked. It
# records its argv, which is how the clip tests assert that the window that
# reached ffmpeg is the one the operator typed.
cat > "$FAKE_BIN/ffmpeg" <<'STUB'
#!/bin/bash
[ -f "$STUB_FAIL_CLIP" ] && { echo "stub ffmpeg: failing on purpose" >&2; exit 1; }
[ -n "${STUB_FFMPEG_ARGS:-}" ] && printf '%s\n' "$@" > "$STUB_FFMPEG_ARGS"
# The output path is always the last argument.
for out in "$@"; do :; done
mkdir -p "$(dirname "$out")"; echo "fake clipped video" > "$out"
STUB
chmod +x "$FAKE_BIN/ffmpeg"
export PATH="$FAKE_BIN:$PATH"

export STUB_FAIL_RECORD="$TESTROOT/fail_record"
export STUB_FAIL_TRANSCRIBE="$TESTROOT/fail_transcribe"
export STUB_LIE_TRANSCRIBE="$TESTROOT/lie_transcribe"
export STUB_FAIL_FRAMES="$TESTROOT/fail_frames"
export STUB_FAIL_SUMMARIZE="$TESTROOT/fail_summarize"
export STUB_PAUSE_SUMMARIZE="$TESTROOT/pause_summarize"
export STUB_RUNSTATE="$STAGING/lib/runstate.py"
export STUB_FAIL_FETCH="$TESTROOT/fail_fetch"
export STUB_SUMMARIZE_ARGS="$TESTROOT/summarize_args.txt"
export STUB_TRANSCRIBE_ARGS="$TESTROOT/transcribe_args.txt"
export STUB_FAIL_KALTURA="$TESTROOT/fail_kaltura"
export STUB_FAIL_CLIP="$TESTROOT/fail_clip"
export STUB_FFMPEG_ARGS="$TESTROOT/ffmpeg_args.txt"

RUNS="$MEETING_BOT_ROOT/runs"
pipeline() { ( cd "$STAGING" && bash ./pipeline.sh "$@" ) ; }
state() { python3 "$STAGING/lib/runstate.py" "$@"; }
latest_run() { python3 "$STAGING/lib/runstate.py" latest --root "$RUNS"; }

echo ""
echo "=================================================================="
echo "1. Input routing — all five input types"
echo "=================================================================="

echo "--- Google Meet URL"
out=$(pipeline "https://meet.google.com/abc-defg-hij" --name "Weekly Standup" 2>&1)
rc=$?
check "meet: exits 0" "$rc" "0"
run=$(latest_run)
check "meet: run id derived from name" "${run%_*_*}" "Weekly_Standup"
for stage in record transcribe frames summarize; do
  check "meet: $stage done" "$(state status --run-dir "$RUNS/$run" --stage $stage)" "done"
done
check "meet: fetch_video skipped" "$(state status --run-dir "$RUNS/$run" --stage fetch_video)" "pending"
[ -f "$RECORDINGS_DIR/$run.mp4" ] && ok "meet: mp4 written" || bad "meet: no mp4"
# The recording is the one irreplaceable artifact; the post-summary media
# sweep only ever touches downloads inside the run dir.
[ -f "$RECORDINGS_DIR/$run.mp4" ] && ok "meet: recording kept after the summary" \
  || bad "meet: the recording was swept"
[ -f "$SUMMARIES_DIR/$run.md" ] && ok "meet: summary written" || bad "meet: no summary"
[ -f "$PDF_DIR/$run.pdf" ] && ok "meet: pdf written to PDF_DIR" || bad "meet: no pdf"

echo "--- Zoom URL"
out=$(pipeline "https://zoom.us/j/1234567890" --name "Client Call" 2>&1)
check "zoom: exits 0" "$?" "0"
run=$(latest_run)
check "zoom: recorded" "$(state status --run-dir "$RUNS/$run" --stage record)" "done"
check "zoom: summarized" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"

echo "--- Local .mp4"
echo "not really a video" > "$TESTROOT/my_lecture.mp4"
out=$(pipeline "$TESTROOT/my_lecture.mp4" 2>&1)
check "local: exits 0" "$?" "0"
run=$(latest_run)
check "local: name derived from filename" "${run%_*_*}" "my_lecture"
check "local: record skipped" "$(state status --run-dir "$RUNS/$run" --stage record)" "pending"
check "local: transcribed" "$(state status --run-dir "$RUNS/$run" --stage transcribe)" "done"
check "local: summarized" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"
[ -f "$TESTROOT/my_lecture.mp4" ] && ok "local: the input file is untouched by the sweep" \
  || bad "local: the sweep deleted the operator's own file"

echo "--- YouTube URL (the one from the request, with its &list= parameter)"
YT="https://www.youtube.com/watch?v=5GAfjAjLKYk&list=PLMvKqhmt0Lp8"
out=$(pipeline "$YT" 2>&1)
check "youtube: exits 0" "$?" "0"
run=$(latest_run)
check "youtube: run id is the video id" "${run%_*_*}" "yt_5GAfjAjLKYk"
check "youtube: record skipped" "$(state status --run-dir "$RUNS/$run" --stage record)" "pending"
check "youtube: video fetched" "$(state status --run-dir "$RUNS/$run" --stage fetch_video)" "done"
check "youtube: summarized" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"
# The download is swept once the summary exists — it was only ever there for
# the frames — and the stage stays done (cleaned) rather than sliding back
# to pending.
[ -e "$RUNS/$run/video.mp4" ] && bad "youtube: video.mp4 kept after the summary" \
  || ok "youtube: video.mp4 swept after the summary"
check "youtube: fetch_video still done after the sweep" \
  "$(state status --run-dir "$RUNS/$run" --stage fetch_video)" "done"
check "youtube: fetch_video marked cleaned" \
  "$(state get --run-dir "$RUNS/$run" --key stages.fetch_video.cleaned)" "True"
echo "$out" | grep -q "\[video\] removed the downloaded media" \
  && ok "youtube: the sweep is reported" || bad "youtube: sweep not reported"
echo "$out" | grep -q -- "--no-playlist" && ok "youtube: &list= did not expand" || ok "youtube: &list= did not expand (single run)"

echo "--- Kaltura embed, pasted as the whole <iframe> tag"
KAL_IFRAME='<iframe id="kaltura_player" src='"'"'https://cdnapisec.kaltura.com/p/2910381/embedPlaykitJs/uiconf_id/52668182?iframeembed=true&amp;entry_id=1_y9jay9sw&amp;config%5Bplayback%5D=%7B%22startTime%22%3A0%7D'"'"' style="width: 608px;height: 402px;border: 0;" allowfullscreen title="2110322 Online Session"></iframe>'
out=$(pipeline "$KAL_IFRAME" 2>&1); rc=$?
check "kaltura: exits 0" "$rc" "0"
run=$(latest_run)
check "kaltura: run id is the entry id" "${run%_*_*}" "kal_1_y9jay9sw"
check "kaltura: record skipped" "$(state status --run-dir "$RUNS/$run" --stage record)" "pending"
check "kaltura: video fetched" "$(state status --run-dir "$RUNS/$run" --stage fetch_video)" "done"
check "kaltura: transcribed" "$(state status --run-dir "$RUNS/$run" --stage transcribe)" "done"
check "kaltura: frames extracted" "$(state status --run-dir "$RUNS/$run" --stage frames)" "done"
check "kaltura: summarized" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"
[ -f "$SUMMARIES_DIR/$run.md" ] && ok "kaltura: summary written" || bad "kaltura: no summary"
# The download is on the critical path for transcribe here, unlike YouTube:
# without captions AssemblyAI needs the media file.
grep -q -- "--media" "$STUB_TRANSCRIBE_ARGS" \
  && ok "kaltura: transcribe was given the downloaded media" \
  || bad "kaltura: transcribe got no --media"
grep -q "video.mp4" "$STUB_TRANSCRIBE_ARGS" \
  && ok "kaltura: --media points at the run's download" \
  || bad "kaltura: --media is not the download"
# The <iframe> blob must not end up in the document's source line.
grep -q -- "--source-url" "$STUB_SUMMARIZE_ARGS" \
  && ok "kaltura: summarize got a source url" || bad "kaltura: no --source-url"
grep -q "<iframe" "$STUB_SUMMARIZE_ARGS" \
  && bad "kaltura: the raw iframe blob reached summarize" \
  || ok "kaltura: the iframe blob was normalised to a URL"
grep -q "entry_id=1_y9jay9sw" "$STUB_SUMMARIZE_ARGS" \
  && ok "kaltura: the source url names the entry" || bad "kaltura: source url lost the entry id"
# The entry's own name, read at fetch time — there is no yt-dlp to ask.
grep -q -- "--title" "$STUB_SUMMARIZE_ARGS" \
  && ok "kaltura: summarize got the entry title" || bad "kaltura: no --title"
grep -q "Stub Kaltura Lecture" "$STUB_SUMMARIZE_ARGS" \
  && ok "kaltura: the title is the entry's own name" || bad "kaltura: wrong title"
[ -f "$RUNS/$run/kaltura.json" ] \
  && ok "kaltura: entry facts cached in the run dir" || bad "kaltura: no kaltura.json"
[ -e "$RUNS/$run/video.mp4" ] && bad "kaltura: video.mp4 kept after the summary" \
  || ok "kaltura: video.mp4 swept after the summary"
check "kaltura: fetch_video still done after the sweep" \
  "$(state status --run-dir "$RUNS/$run" --stage fetch_video)" "done"

echo "--- Kaltura embed, given as just the src URL"
KAL_URL="https://cdnapisec.kaltura.com/p/2910381/embedPlaykitJs/uiconf_id/52668182?iframeembed=true&entry_id=1_y9jay9sw"
out=$(pipeline "$KAL_URL" --force 2>&1)
check "kaltura url: exits 0" "$?" "0"
run=$(latest_run)
check "kaltura url: same run id as the iframe form" "${run%_*_*}" "kal_1_y9jay9sw"
check "kaltura url: summarized" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"

echo "--- Kaltura entry that has its own captions"
STUB_KALTURA_CAPTIONS=1 out=$(STUB_KALTURA_CAPTIONS=1 pipeline "$KAL_URL" --force 2>&1)
check "kaltura captions: exits 0" "$?" "0"
run=$(latest_run)
check "kaltura captions: transcribed" "$(state status --run-dir "$RUNS/$run" --stage transcribe)" "done"
# The video is still fetched — frames need it either way.
check "kaltura captions: video still fetched" "$(state status --run-dir "$RUNS/$run" --stage fetch_video)" "done"

echo "--- Kaltura entry that cannot be reached (needs an LMS login)"
touch "$STUB_FAIL_KALTURA"
out=$(pipeline "$KAL_URL" --force 2>&1); rc=$?
rm -f "$STUB_FAIL_KALTURA"
check "kaltura unreachable: exits nonzero" "$([ "$rc" -ne 0 ] && echo yes || echo no)" "yes"
run=$(latest_run)
check "kaltura unreachable: fetch_video failed" "$(state status --run-dir "$RUNS/$run" --stage fetch_video)" "failed"
# Fail before anything expensive: no transcript was paid for.
check "kaltura unreachable: transcribe never ran" "$(state status --run-dir "$RUNS/$run" --stage transcribe)" "pending"
check "kaltura unreachable: summarize never ran" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "pending"
echo "$out" | grep -q "Resume with" && ok "kaltura unreachable: says how to resume" || bad "kaltura unreachable: no resume hint"

echo "--- Kaltura resume picks up the finished download"
rm -f "$STUB_FAIL_KALTURA"
out=$(pipeline --run-id "$run" 2>&1)
check "kaltura resume: exits 0" "$?" "0"
check "kaltura resume: completes" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"

echo "--- A finished Kaltura run re-invoked by --run-id does not download again"
# The ahead-of-branches fetch used to run unconditionally; with the download
# swept after the summary that would pull 446MB back for a run with nothing
# left to do.
out=$(pipeline --run-id "$run" 2>&1)
check "kaltura finished: exits 0" "$?" "0"
echo "$out" | grep -q "downloading again" && bad "kaltura finished: re-downloaded the entry" \
  || ok "kaltura finished: no re-download"
[ -e "$RUNS/$run/video.mp4" ] && bad "kaltura finished: a video.mp4 reappeared" \
  || ok "kaltura finished: run dir still has no video"

echo "--- Unrecognized input"
out=$(pipeline "not-a-real-thing" 2>&1); rc=$?
check "bad input: exits 1" "$rc" "1"
echo "$out" | grep -q "unrecognized input" && ok "bad input: clean error" || bad "bad input: no clear error"

echo ""
echo "=================================================================="
echo "2. Multiple YouTube links at once"
echo "=================================================================="
before=$(ls -1 "$RUNS" | wc -l)
out=$(pipeline "https://www.youtube.com/watch?v=aaaaaaaaaaa" \
               "https://youtu.be/bbbbbbbbbbb" \
               "https://www.youtube.com/watch?v=ccccccccccc" \
               --jobs 3 --combine "$TESTROOT/chapter.md" 2>&1)
check "multi: exits 0" "$?" "0"
after=$(ls -1 "$RUNS" | wc -l)
# Three members plus the combine run.
check "multi: created 3 member runs and 1 combine run" "$((after - before))" "4"
for vid in aaaaaaaaaaa bbbbbbbbbbb ccccccccccc; do
  ls -d "$RUNS/yt_${vid}_"* >/dev/null 2>&1 && ok "multi: run for $vid" || bad "multi: no run for $vid"
done
[ -f "$TESTROOT/chapter.md" ] && ok "multi: combined file written" || bad "multi: no combined file"
check "multi: one title in combined file" \
  "$(grep -c '^# Stub combined' "$TESTROOT/chapter.md")" "1"
echo "$out" | grep -q "3 run(s), up to 3 at a time" && ok "multi: honored --jobs 3" || bad "multi: --jobs not honored"

# --- The combined summary is ONE summarize call over every member ----------
# --combine no longer staples three summaries together: the members stop after
# transcribe + frames (their summarize stage stays pending, on purpose), and a
# fourth run — the combine run — hands every transcript and manifest to
# summarize.py --parts in input order.
for vid in aaaaaaaaaaa bbbbbbbbbbb ccccccccccc; do
  for d in "$RUNS"/yt_${vid}_*; do
    check "multi: $vid transcribed" "$(state status --run-dir "$d" --stage transcribe)" "done"
    check "multi: $vid summarize left pending (combine run owns it)" \
      "$(state status --run-dir "$d" --stage summarize)" "pending"
    [ -f "$SUMMARIES_DIR/$(basename "$d").md" ] \
      && bad "multi: $vid got an individual summary anyway" \
      || ok "multi: $vid has no individual summary"
  done
done
combine_run="$(ls -d "$RUNS"/combine_3x_* 2>/dev/null | head -n 1)"
[ -n "$combine_run" ] && ok "multi: combine run created ($(basename "$combine_run"))" \
  || bad "multi: no combine run directory"
check "multi: combine run summarize done" \
  "$(state status --run-dir "$combine_run" --stage summarize)" "done"
check "multi: combine run records the markdown" \
  "$(state get --run-dir "$combine_run" --key stages.summarize.artifacts.md)" "$TESTROOT/chapter.md"
# Members point back at the combine run, so --resume-all leaves them alone.
for d in "$RUNS"/yt_aaaaaaaaaaa_*; do
  check "multi: member records its combine run" \
    "$(state get --run-dir "$d" --key combined_into)" "$(basename "$combine_run")"
done
# The parts file is the whole contract with summarize.py: every member, in
# INPUT order (runs finish out of order at --jobs 3), each with its own
# transcript and manifest.
parts="$combine_run/parts.json"
[ -f "$parts" ] && ok "multi: parts.json written" || bad "multi: no parts.json"
check "multi: parts.json lists 3 videos" \
  "$(python3 -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["parts"]))' "$parts")" "3"
check "multi: parts in input order" \
  "$(python3 -c 'import json,sys; print(" ".join(p["source"].split("=")[-1].split("/")[-1] for p in json.load(open(sys.argv[1]))["parts"]))' "$parts")" \
  "aaaaaaaaaaa bbbbbbbbbbb ccccccccccc"
check "multi: each part has its own manifest" \
  "$(python3 -c 'import json,sys; ps=json.load(open(sys.argv[1]))["parts"]; print(len({p["frames_manifest"] for p in ps}))' "$parts")" "3"
grep -q "^--parts$" "$STUB_SUMMARIZE_ARGS" && ok "multi: summarize.py called with --parts" \
  || bad "multi: summarize.py not called with --parts"
grep -q "^--pdf-out$" "$STUB_SUMMARIZE_ARGS" && ok "multi: combined pdf requested" \
  || bad "multi: no --pdf-out for the combined pdf"
check "multi: combined document names all three videos" \
  "$(grep -c '^video [0-9]: ' "$TESTROOT/chapter.md")" "3"
[ -f "$TESTROOT/chapter.pdf" ] && ok "multi: combined pdf written" || bad "multi: no combined pdf"
echo "$out" | grep -q "(combined summary)" && ok "multi: report names the combined summary" \
  || bad "multi: report silent about the combined summary"
# The members' frames are swept by the combine run once the PDF exists.
leftover=0
for vid in aaaaaaaaaaa bbbbbbbbbbb ccccccccccc; do
  for d in "$FRAMES_DIR/yt_${vid}_"*; do
    [ -d "$d" ] && leftover=$((leftover + 1))
  done
done
check "multi: members' frames swept after the combined render" "$leftover" "0"
leftover=0
for vid in aaaaaaaaaaa bbbbbbbbbbb ccccccccccc; do
  for f in "$RUNS/yt_${vid}_"*/video.mp4; do
    [ -e "$f" ] && leftover=$((leftover + 1))
  done
done
check "multi: members' downloads swept after the combined render" "$leftover" "0"

echo "--- Re-running the same command resumes the combined summary, not a new one"
: > "$STUB_SUMMARIZE_ARGS"
out=$(pipeline "https://www.youtube.com/watch?v=aaaaaaaaaaa" \
               "https://youtu.be/bbbbbbbbbbb" \
               "https://www.youtube.com/watch?v=ccccccccccc" \
               --jobs 3 --combine "$TESTROOT/chapter.md" 2>&1)
check "multi resume: exits 0" "$?" "0"
check "multi resume: no new combine run" "$(ls -d "$RUNS"/combine_3x_* | wc -l)" "1"
[ -s "$STUB_SUMMARIZE_ARGS" ] && bad "multi resume: summarize ran again" \
  || ok "multi resume: summarize not re-run (already done)"
echo "$out" | grep -q "already done" && ok "multi resume: says summarize was done" \
  || bad "multi resume: no 'already done'"

echo "--- --run-id on the combine run after --force re-extracts swept frames"
: > "$STUB_SUMMARIZE_ARGS"
out=$(pipeline --run-id "$(basename "$combine_run")" --force 2>&1)
check "combine --force: exits 0" "$?" "0"
grep -q "^--parts$" "$STUB_SUMMARIZE_ARGS" && ok "combine --force: summarized again" \
  || bad "combine --force: summarize did not run"
echo "$out" | grep -q "frames were swept" && ok "combine --force: re-extracted the swept frames" \
  || bad "combine --force: did not notice the swept frames"
echo "$out" | grep -q "\[fetch_video\] swept after the last summary" \
  && ok "combine --force: fetched the swept downloads again" \
  || bad "combine --force: did not re-download for the re-extraction"
leftover=0
for vid in aaaaaaaaaaa bbbbbbbbbbb ccccccccccc; do
  for f in "$RUNS/yt_${vid}_"*/video.mp4; do
    [ -e "$f" ] && leftover=$((leftover + 1))
  done
done
check "combine --force: the re-fetched downloads are swept again" "$leftover" "0"

echo "--- A failed member blocks the combined summary, and the same command resumes both"
touch "$STUB_FAIL_TRANSCRIBE"
: > "$STUB_SUMMARIZE_ARGS"
out=$(pipeline "https://www.youtube.com/watch?v=hhhhhhhhhhh" \
               "https://youtu.be/iiiiiiiiiii" \
               --combine "$TESTROOT/partial.md" 2>&1)
check "member fail: exits 1" "$?" "1"
[ -s "$STUB_SUMMARIZE_ARGS" ] && bad "member fail: summarize ran on an incomplete set" \
  || ok "member fail: summarize not attempted"
echo "$out" | grep -q "Combined summary not attempted" && ok "member fail: says why" \
  || bad "member fail: no explanation"
[ -f "$TESTROOT/partial.md" ] && bad "member fail: wrote a combined file anyway" \
  || ok "member fail: no combined file"
rm -f "$STUB_FAIL_TRANSCRIBE"
before=$(ls -1 "$RUNS" | wc -l)
out=$(pipeline "https://www.youtube.com/watch?v=hhhhhhhhhhh" \
               "https://youtu.be/iiiiiiiiiii" \
               --combine "$TESTROOT/partial.md" 2>&1)
check "member fail resume: exits 0" "$?" "0"
after=$(ls -1 "$RUNS" | wc -l)
check "member fail resume: no new runs (members and combine run resumed)" "$((after - before))" "0"
[ -f "$TESTROOT/partial.md" ] && ok "member fail resume: combined file written" \
  || bad "member fail resume: no combined file"

echo "--- --resume-all leaves combine members to their combine run"
# Make a fresh set whose combine summarize fails, then --resume-all.
touch "$STUB_FAIL_SUMMARIZE"
out=$(pipeline "https://www.youtube.com/watch?v=jjjjjjjjjjj" \
               --combine "$TESTROOT/ra.md" 2>&1)
check "resume-all setup: combine summarize failed" "$?" "1"
rm -f "$STUB_FAIL_SUMMARIZE"
: > "$STUB_SUMMARIZE_ARGS"
out=$(pipeline --resume-all 2>&1)
check "resume-all: exits 0" "$?" "0"
echo "$out" | grep -q "resumed through it" && ok "resume-all: skipped the member" \
  || bad "resume-all: did not skip the member"
grep -q "^--parts$" "$STUB_SUMMARIZE_ARGS" && ok "resume-all: ran the combine run" \
  || bad "resume-all: combine run not resumed"
for d in "$RUNS"/yt_jjjjjjjjjjj_*; do
  check "resume-all: member still has no individual summary" \
    "$(state status --run-dir "$d" --stage summarize)" "pending"
done
[ -f "$TESTROOT/ra.md" ] && ok "resume-all: combined file written" || bad "resume-all: no combined file"

echo "--- --no-combine-pdf writes only the markdown"
: > "$STUB_SUMMARIZE_ARGS"
out=$(pipeline "https://www.youtube.com/watch?v=ddddddddddd" \
               "https://youtu.be/eeeeeeeeeee" \
               --combine "$TESTROOT/nopdf.md" --no-combine-pdf 2>&1)
check "no-combine-pdf: exits 0" "$?" "0"
[ -f "$TESTROOT/nopdf.md" ] && ok "no-combine-pdf: markdown written" \
  || bad "no-combine-pdf: no markdown"
[ -f "$TESTROOT/nopdf.pdf" ] && bad "no-combine-pdf: wrote a pdf anyway" \
  || ok "no-combine-pdf: no pdf written"
grep -q "^--no-pdf$" "$STUB_SUMMARIZE_ARGS" && ok "no-combine-pdf: summarize told --no-pdf" \
  || bad "no-combine-pdf: summarize not told --no-pdf"
# No PDF to wait for, so the frames go straight away.
leftover=0
for d in "$FRAMES_DIR"/yt_ddddddddddd_* "$FRAMES_DIR"/yt_eeeeeeeeeee_*; do
  [ -d "$d" ] && leftover=$((leftover + 1))
done
check "no-combine-pdf: members' frames swept" "$leftover" "0"

echo "--- --combine-pdf names the file"
out=$(pipeline "https://www.youtube.com/watch?v=fffffffffff" \
               --combine "$TESTROOT/named.md" \
               --combine-pdf "$TESTROOT/somewhere else.pdf" 2>&1)
check "combine-pdf: exits 0" "$?" "0"
[ -f "$TESTROOT/somewhere else.pdf" ] \
  && ok "combine-pdf: honored the path (with a space in it)" \
  || bad "combine-pdf: path not used"

echo "--- KEEP_FRAMES=1 survives a combined render"
out=$(KEEP_FRAMES=1 pipeline "https://youtu.be/ggggggggggg" \
               --combine "$TESTROOT/keep.md" 2>&1)
check "keep-frames: exits 0" "$?" "0"
kept=0
for d in "$FRAMES_DIR/yt_ggggggggggg_"*; do [ -d "$d" ] && kept=1; done
check "keep-frames: frames kept" "$kept" "1"
kept=0
for f in "$RUNS/yt_ggggggggggg_"*/video.mp4; do [ -e "$f" ] && kept=1; done
check "keep-frames: the download is kept too" "$kept" "1"

echo "--- KEEP_FRAMES=1 keeps a single run's download as well"
out=$(KEEP_FRAMES=1 pipeline "https://youtu.be/keepvideo01" 2>&1)
check "keep-video: exits 0" "$?" "0"
run=$(latest_run)
[ -f "$RUNS/$run/video.mp4" ] && ok "keep-video: video.mp4 kept" || bad "keep-video: video.mp4 swept"
[ -d "$FRAMES_DIR/$run" ] && ok "keep-video: frames kept" || bad "keep-video: frames swept"

echo "--- --combine refuses the resume-only forms"
out=$(pipeline --resume-last --combine "$TESTROOT/x.md" 2>&1)
check "combine + --resume-last: exits 1" "$?" "1"
echo "$out" | grep -q "takes inputs" && ok "combine + --resume-last: clear error" \
  || bad "combine + --resume-last: no clear error"

echo "--- --from-file"
cat > "$TESTROOT/links.txt" <<EOF
# a comment line, and a blank one below

https://www.youtube.com/watch?v=ddddddddddd
https://www.youtube.com/watch?v=eeeeeeeeeee
EOF
out=$(pipeline --from-file "$TESTROOT/links.txt" --jobs 2 2>&1)
check "from-file: exits 0" "$?" "0"
echo "$out" | grep -q "2 run(s)" && ok "from-file: comments/blanks skipped" || bad "from-file: wrong input count"

echo ""
echo "=================================================================="
echo "3. Resume — the point of the whole exercise"
echo "=================================================================="

echo "--- A stage that exits 0 without its artifacts is not 'done'"
touch "$STUB_LIE_TRANSCRIBE"
LIAR="https://www.youtube.com/watch?v=liar00000001"
out=$(pipeline "$LIAR" 2>&1); rc=$?
check "liar: run exits 1" "$rc" "1"
run=$(latest_run)
check "liar: transcribe not marked done" \
  "$(state status --run-dir "$RUNS/$run" --stage transcribe)" "failed"
check "liar: summarize never ran" \
  "$(state status --run-dir "$RUNS/$run" --stage summarize)" "pending"
echo "$out" | grep -q "does not exist" \
  && ok "liar: says which artifact was missing" \
  || bad "liar: no missing-artifact message"
rm -f "$STUB_LIE_TRANSCRIBE"

echo "--- A run that fails at summarize keeps its earlier work"
touch "$STUB_FAIL_SUMMARIZE"
LECTURE="https://www.youtube.com/watch?v=resume00001"
out=$(pipeline "$LECTURE" 2>&1); rc=$?
check "resume: failed run exits 1" "$rc" "1"
run=$(latest_run)
check "resume: transcribe survived" "$(state status --run-dir "$RUNS/$run" --stage transcribe)" "done"
check "resume: frames survived" "$(state status --run-dir "$RUNS/$run" --stage frames)" "done"
check "resume: summarize marked failed" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "failed"
[ -f "$RUNS/$run/video.mp4" ] && ok "resume: the download survived the failure" \
  || bad "resume: the download was swept before the summary existed"
state show --run-dir "$RUNS/$run" | grep -q "error:" && ok "resume: error recorded in state" || bad "resume: no error recorded"

echo "--- Re-running the same command resumes instead of starting over"
rm -f "$STUB_FAIL_SUMMARIZE"
out=$(pipeline "$LECTURE" 2>&1); rc=$?
check "resume: second attempt exits 0" "$rc" "0"
check "resume: same run id reused" "$(latest_run)" "$run"
echo "$out" | grep -q "Resuming unfinished run" && ok "resume: announced the resume" || bad "resume: did not announce"
echo "$out" | grep -q "\[transcribe\] already done" && ok "resume: skipped transcribe" || bad "resume: re-ran transcribe"
echo "$out" | grep -q "\[frames\] already done" && ok "resume: skipped frames" || bad "resume: re-ran frames"
check "resume: summarize now done" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"
check "resume: transcribe attempted once only" \
  "$(state get --run-dir "$RUNS/$run" --key stages.transcribe.attempts)" "1"
[ -e "$RUNS/$run/video.mp4" ] && bad "resume: the download outlived the summary" \
  || ok "resume: the download swept once the summary existed"

echo "--- A finished run re-invoked by --run-id neither downloads nor extracts again"
out=$(pipeline --run-id "$run" 2>&1)
check "finished: exits 0" "$?" "0"
echo "$out" | grep -q "downloading again" && bad "finished: re-downloaded" || ok "finished: no re-download"
echo "$out" | grep -q "extracting them again" && bad "finished: re-extracted frames" \
  || ok "finished: no re-extraction"
check "finished: fetch_video attempted once only" \
  "$(state get --run-dir "$RUNS/$run" --key stages.fetch_video.attempts)" "1"

echo "--- Deleting an artifact makes that stage run again"
rm -f "$TRANSCRIPTS_DIR/$run.txt"
check "stale: transcribe reports pending again" \
  "$(state status --run-dir "$RUNS/$run" --stage transcribe)" "pending"

echo "--- --force starts clean"
out=$(pipeline --run-id "$run" --force 2>&1)
check "force: exits 0" "$?" "0"
check "force: transcribe re-run (attempts reset to 1)" \
  "$(state get --run-dir "$RUNS/$run" --key stages.transcribe.attempts)" "1"
echo "$out" | grep -q "discarding previous stage results" && ok "force: announced" || bad "force: not announced"
check "force: the swept download was fetched again" \
  "$(state get --run-dir "$RUNS/$run" --key stages.fetch_video.attempts)" "1"
[ -e "$RUNS/$run/video.mp4" ] && bad "force: the re-fetched download was not swept" \
  || ok "force: the re-fetched download was swept after the summary"

echo "--- A completed run is not resumed; a fresh one starts"
out=$(pipeline "$LECTURE" 2>&1)
check "fresh: exits 0" "$?" "0"
[ "$(latest_run)" != "$run" ] && ok "fresh: new run id for a finished input" || bad "fresh: reused a finished run"

echo "--- --resume-all picks up only unfinished runs"
touch "$STUB_FAIL_SUMMARIZE"
pipeline "https://www.youtube.com/watch?v=broken00001" >/dev/null 2>&1
pipeline "https://www.youtube.com/watch?v=broken00002" >/dev/null 2>&1
rm -f "$STUB_FAIL_SUMMARIZE"
out=$(pipeline --resume-all --jobs 2 2>&1)
check "resume-all: exits 0" "$?" "0"
echo "$out" | grep -qE "Resuming [0-9]+ unfinished run" && ok "resume-all: found the broken runs" || bad "resume-all: found nothing"
for vid in broken00001 broken00002; do
  d=$(ls -d "$RUNS/yt_${vid}_"* | head -1)
  check "resume-all: $vid completed" "$(state status --run-dir "$d" --stage summarize)" "done"
done

echo "--- A summarize stage paused on the Claude usage window"
# The stub records a reset time an hour away and exits 75, the way
# summarize.py does when the in-process wait gives up.
echo $(( $(date +%s) + 3600 )) > "$STUB_PAUSE_SUMMARIZE"
LECTURE="https://www.youtube.com/watch?v=paused00001"
out=$(pipeline "$LECTURE" 2>&1); rc=$?
check "pause: pipeline exits 75, not 1" "$rc" "75"
prun=$(latest_run)
check "pause: summarize is failed (so a resume re-runs it)" \
  "$(state status --run-dir "$RUNS/$prun" --stage summarize)" "failed"
check "pause: frames survived" "$(state status --run-dir "$RUNS/$prun" --stage frames)" "done"
[ -n "$(state get --run-dir "$RUNS/$prun" --key stages.summarize.rate_limited.resets_at 2>/dev/null)" ] \
  && ok "pause: reset time recorded in state.json" || bad "pause: no reset time in state"
echo "$out" | grep -q "PAUSED $prun" && ok "pause: reported as PAUSED, not FAIL" || bad "pause: not reported as PAUSED"
echo "$out" | grep -q "  FAIL  $prun" && bad "pause: also reported as FAIL" || ok "pause: not reported as FAIL"
echo "$out" | grep -q "waiting for the Claude usage window" && ok "pause: the footer explains" || bad "pause: no explanation"
state show --run-dir "$RUNS/$prun" | grep -q "paused: Claude usage window" \
  && ok "pause: --status explains the pause" || bad "pause: --status does not mention it"

echo "--- --resume-all leaves a paused run alone until its reset"
out=$(pipeline --resume-all 2>&1); rc=$?
check "pause: --resume-all exits 0 with nothing to do" "$rc" "0"
echo "$out" | grep -q "$prun: paused until" && ok "pause: --resume-all names the paused run and the time" \
  || bad "pause: --resume-all did not mention the pause"
echo "$out" | grep -q "waiting for the Claude usage window" && ok "pause: 'nothing to resume yet'" \
  || bad "pause: wrong summary line"
check "pause: summarize not attempted again" \
  "$(state get --run-dir "$RUNS/$prun" --key stages.summarize.attempts)" "1"

echo "--- Once the reset has passed, --resume-all finishes it"
# Rewrite the recorded reset into the past and let the stub succeed.
state annotate --run-dir "$RUNS/$prun" --stage summarize \
  --set 'rate_limited={"window": "five_hour", "resets_at": 1000000000}'
rm -f "$STUB_PAUSE_SUMMARIZE"
out=$(pipeline --resume-all 2>&1); rc=$?
check "pause: --resume-all exits 0" "$rc" "0"
check "pause: summarize now done" "$(state status --run-dir "$RUNS/$prun" --stage summarize)" "done"
[ -z "$(state get --run-dir "$RUNS/$prun" --key stages.summarize.rate_limited 2>/dev/null)" ] \
  && ok "pause: the pause is cleared on completion" || bad "pause: rate_limited left behind"
echo "$out" | grep -q "\[transcribe\] already done" && ok "pause: transcribe not re-run" || bad "pause: transcribe re-ran"

echo "--- The same command resumes a paused run too"
echo $(( $(date +%s) + 3600 )) > "$STUB_PAUSE_SUMMARIZE"
pipeline "https://www.youtube.com/watch?v=paused00002" >/dev/null 2>&1
prun2=$(latest_run)
rm -f "$STUB_PAUSE_SUMMARIZE"
out=$(pipeline "https://www.youtube.com/watch?v=paused00002" 2>&1); rc=$?
check "pause: explicit re-run exits 0" "$rc" "0"
check "pause: same run id" "$(latest_run)" "$prun2"
check "pause: done" "$(state status --run-dir "$RUNS/$prun2" --stage summarize)" "done"

echo "--- A failing mid-stage still reports the other branch"
touch "$STUB_FAIL_TRANSCRIBE"
out=$(pipeline "https://www.youtube.com/watch?v=halffail001" 2>&1); rc=$?
check "branch: exits 1" "$rc" "1"
run=$(latest_run)
check "branch: transcribe failed" "$(state status --run-dir "$RUNS/$run" --stage transcribe)" "failed"
check "branch: frames still succeeded" "$(state status --run-dir "$RUNS/$run" --stage frames)" "done"
rm -f "$STUB_FAIL_TRANSCRIBE"
out=$(pipeline --run-id "$run" 2>&1)
check "branch: resume completes it" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"

echo ""
echo "=================================================================="
echo "4. Concurrency safety"
echo "=================================================================="

echo "--- Two processes cannot work the same run"
touch "$STUB_FAIL_SUMMARIZE"
pipeline "https://www.youtube.com/watch?v=locktest0001" >/dev/null 2>&1
rm -f "$STUB_FAIL_SUMMARIZE"
run=$(latest_run)
mkdir -p "$RUNS/$run/run.lock"
echo $$ > "$RUNS/$run/run.lock/pid"   # a live PID: this shell
out=$(pipeline --run-id "$run" 2>&1); rc=$?
echo "$out" | grep -q "already being processed" && ok "lock: refused a concurrent run" || bad "lock: allowed a double run"
rm -rf "$RUNS/$run/run.lock"

echo "--- A stale lock (dead owner) is taken over, not fatal"
mkdir -p "$RUNS/$run/run.lock"
echo "999999" > "$RUNS/$run/run.lock/pid"   # a PID that does not exist
out=$(pipeline --run-id "$run" 2>&1)
echo "$out" | grep -q "Taking over a stale lock" && ok "lock: took over a stale lock" || bad "lock: did not recover"
check "lock: run completed after takeover" \
  "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"

echo "--- Per-run kill sentinels are isolated"
a=$(ls -d "$RUNS"/*/ | head -1); b=$(ls -d "$RUNS"/*/ | tail -1)
touch "${a}kill"
[ ! -f "${b}kill" ] && ok "kill: sentinel did not leak to another run" || bad "kill: sentinel leaked"
rm -f "${a}kill"

echo ""
echo "=================================================================="
echo "5. Inspection commands"
echo "=================================================================="
# Capture first: piping straight into `grep -q` makes grep exit on the first
# match, SIGPIPEs the producer, and (under pipefail) fails the assertion even
# though the command worked.
out=$(pipeline --list 2>&1)
echo "$out" | grep -q "RUN ID" && ok "--list prints a table" || bad "--list broken"
run=$(latest_run)
out=$(pipeline --status "$run" 2>&1)
echo "$out" | grep -q "stages:" && ok "--status prints detail" || bad "--status broken"
pipeline --status "no-such-run" >/dev/null 2>&1
[ $? -ne 0 ] && ok "--status on a bad id exits non-zero" || bad "--status swallowed a bad id"

echo ""
echo "=================================================================="
echo "6. Legacy positional form still works"
echo "=================================================================="
out=$(pipeline "https://meet.google.com/xyz-legacy-abc" "Legacy Meeting" "Bot Name" "en" 2>&1)
check "legacy: exits 0" "$?" "0"
run=$(latest_run)
check "legacy: name parsed" "${run%_*_*}" "Legacy_Meeting"
check "legacy: language parsed" "$(state get --run-dir "$RUNS/$run" --key language)" "en"
check "legacy: display name parsed" "$(state get --run-dir "$RUNS/$run" --key display_name)" "Bot Name"

out=$(pipeline "https://youtu.be/aaaaaaaaaaa" "https://youtu.be/bbbbbbbbbbb" "Some Name" 2>&1); rc=$?
check "legacy: refuses ambiguous mix" "$rc" "1"

echo ""
echo "=================================================================="
echo "7. Cross-session queue (several ./pipeline.sh sessions at once)"
echo "=================================================================="

run_two_sessions() {
  # Two INDEPENDENT pipeline.sh invocations, started at the same moment —
  # the thing --jobs cannot coordinate.
  rm -f "$TESTROOT/trace.txt"
  export STUB_TRACE="$TESTROOT/trace.txt"
  export STUB_TRANSCRIBE_SECONDS=1
  pipeline "https://www.youtube.com/watch?v=sessAAAAAAA" >/dev/null 2>&1 &
  local p1=$!
  pipeline "https://www.youtube.com/watch?v=sessBBBBBBB" >/dev/null 2>&1 &
  local p2=$!
  wait $p1; wait $p2
  unset STUB_TRACE STUB_TRANSCRIBE_SECONDS
}

echo "--- with no QUEUE_SLOTS set, both sessions transcribe at once"
run_two_sessions
overlap=$(awk 'NR<=2 && /^start/ {n++} END {print n+0}' "$TESTROOT/trace.txt")
check "queue off: the two sessions overlapped" "$overlap" "2"

echo "--- with QUEUE_SLOTS_TRANSCRIBE=1, they take turns"
export QUEUE_SLOTS_TRANSCRIBE=1
export QUEUE_POLL_SECONDS=0.2
# Fresh inputs so these are new runs rather than resumes of the ones above.
rm -f "$TESTROOT/trace.txt"
export STUB_TRACE="$TESTROOT/trace.txt"
export STUB_TRANSCRIBE_SECONDS=1
pipeline "https://www.youtube.com/watch?v=queueAAAAAA" >/dev/null 2>&1 &
q1=$!
pipeline "https://www.youtube.com/watch?v=queueBBBBBB" >/dev/null 2>&1 &
q2=$!
wait $q1; wait $q2
unset STUB_TRACE STUB_TRANSCRIBE_SECONDS

# Serialized means every "start" is immediately followed by its own "end".
serialized=1
prev_event=""; prev_name=""
while read -r event name; do
  if [ "$event" = "start" ] && [ "$prev_event" = "start" ]; then serialized=0; fi
  prev_event="$event"; prev_name="$name"
done < "$TESTROOT/trace.txt"
check "queue on: transcribe was serialized across sessions" "$serialized" "1"
check "queue on: both sessions still completed" \
  "$(grep -c '^end' "$TESTROOT/trace.txt")" "2"

echo "--- the queue file records who held the slot"
python3 "$STAGING/lib/slotqueue.py" status --component transcribe 2>&1 | grep -q "transcribe" \
  && ok "queue: status reports the component" || bad "queue: status broken"

echo "--- slots are released after the runs finish"
held=$(python3 "$STAGING/lib/slotqueue.py" status --component transcribe 2>&1 | grep -c "running" || true)
check "queue: no slot left held" "$held" "0"

unset QUEUE_SLOTS_TRANSCRIBE QUEUE_POLL_SECONDS

echo ""
echo "=================================================================="
echo "8. Output directories, PDF and reference material"
echo "=================================================================="

echo "--- Every output directory is independent, and required"
out=$( cd "$STAGING" && env -u SUMMARIES_DIR bash ./pipeline.sh \
       "https://www.youtube.com/watch?v=nopath00001" 2>&1 )
rc=$?
check "paths: a missing SUMMARIES_DIR fails the run" "$rc" "1"
echo "$out" | grep -q "SUMMARIES_DIR" \
  && ok "paths: the error names the missing variable" \
  || bad "paths: error does not name the variable"

echo "--- Artifacts land in the configured directories, spaces and all"
run=$(ls -1t "$RUNS" | head -1)
prev=$(ls -1t "$RUNS" | head -1)
out=$(pipeline "https://www.youtube.com/watch?v=paths0000001" 2>&1)
check "paths: exits 0" "$?" "0"
run=$(latest_run)
check "paths: md artifact recorded" \
  "$(state get --run-dir "$RUNS/$run" --key stages.summarize.artifacts.md)" \
  "$SUMMARIES_DIR/$run.md"
check "paths: pdf artifact recorded" \
  "$(state get --run-dir "$RUNS/$run" --key stages.summarize.artifacts.pdf)" \
  "$PDF_DIR/$run.pdf"
case "$SUMMARIES_DIR" in *" "*) ok "paths: output path contains a space" ;;
  *) bad "paths: test root has no space in it" ;; esac

echo "--- --no-pdf: markdown only, and the stage still succeeds"
export STUB_SUMMARIZE_NO_PDF=1
out=$(pipeline "https://www.youtube.com/watch?v=nopdf0000001" 2>&1)
check "nopdf: exits 0" "$?" "0"
run=$(latest_run)
check "nopdf: summarize done" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"
check "nopdf: md still recorded" \
  "$(state get --run-dir "$RUNS/$run" --key stages.summarize.artifacts.md)" \
  "$SUMMARIES_DIR/$run.md"
state get --run-dir "$RUNS/$run" --key stages.summarize.artifacts.pdf >/dev/null 2>&1 \
  && bad "nopdf: recorded a pdf that was never written" \
  || ok "nopdf: no pdf artifact recorded"
unset STUB_SUMMARIZE_NO_PDF

echo "--- --no-markdown: PDF only"
export STUB_SUMMARIZE_NO_MARKDOWN=1
out=$(pipeline "https://www.youtube.com/watch?v=nomd00000001" 2>&1)
check "nomd: exits 0" "$?" "0"
run=$(latest_run)
check "nomd: pdf recorded" \
  "$(state get --run-dir "$RUNS/$run" --key stages.summarize.artifacts.pdf)" \
  "$PDF_DIR/$run.pdf"
state get --run-dir "$RUNS/$run" --key stages.summarize.artifacts.md >/dev/null 2>&1 \
  && bad "nomd: recorded a markdown file that was never written" \
  || ok "nomd: no md artifact recorded"
unset STUB_SUMMARIZE_NO_MARKDOWN

echo "--- Neither output written is a failed stage, not a silent success"
export STUB_SUMMARIZE_NO_PDF=1
export STUB_SUMMARIZE_NO_MARKDOWN=1
out=$(pipeline "https://www.youtube.com/watch?v=nothing00001" 2>&1); rc=$?
check "empty: run exits 1" "$rc" "1"
run=$(latest_run)
check "empty: summarize marked failed" \
  "$(state status --run-dir "$RUNS/$run" --stage summarize)" "failed"
echo "$out" | grep -q "produced no output files" \
  && ok "empty: says what went wrong" || bad "empty: unclear error"
unset STUB_SUMMARIZE_NO_PDF STUB_SUMMARIZE_NO_MARKDOWN

echo "--- --resources reaches summarize and survives a resume"
mkdir -p "$TESTROOT/slides deck"
echo "# Week 4 slides" > "$TESTROOT/slides deck/week4.md"
touch "$STUB_FAIL_SUMMARIZE"
pipeline "https://www.youtube.com/watch?v=res000000001" \
         --resources "$TESTROOT/slides deck" >/dev/null 2>&1
run=$(latest_run)
check "resources: recorded in state.json" \
  "$(state get --run-dir "$RUNS/$run" --key resources)" "$TESTROOT/slides deck"
rm -f "$STUB_FAIL_SUMMARIZE"
out=$(pipeline --run-id "$run" 2>&1)
check "resources: resumed run exits 0" "$?" "0"
grep -qx -- "--resources" "$STUB_SUMMARIZE_ARGS" \
  && ok "resources: passed to summarize on the resume" \
  || bad "resources: not passed to summarize"
grep -qx -- "$TESTROOT/slides deck" "$STUB_SUMMARIZE_ARGS" \
  && ok "resources: spec with a space passed intact" \
  || bad "resources: spec was mangled"

echo "--- Two --resources are both threaded through"
out=$(pipeline "https://www.youtube.com/watch?v=res000000002" \
      --resources "$TESTROOT/slides deck" \
      --resources "https://github.com/acme/course@wk4" 2>&1)
check "resources: exits 0" "$?" "0"
check "resources: both specs recorded" \
  "$(state get --run-dir "$RUNS/$(latest_run)" --key resources | wc -l)" "2"
check "resources: both passed to summarize" \
  "$(grep -cx -- "--resources" "$STUB_SUMMARIZE_ARGS")" "2"


echo ""
echo "=================================================================="
echo "N. --clip: summarize only part of a video"
echo "=================================================================="

echo "--- YouTube: the window is cut after the download, before frames"
rm -f "$STUB_FFMPEG_ARGS"
out=$(pipeline "https://www.youtube.com/watch?v=clip000000001" \
      --clip 00:05:00-01:30:00 2>&1)
check "clip/yt: exits 0" "$?" "0"
run=$(latest_run)
check "clip/yt: clip stage done" "$(state status --run-dir "$RUNS/$run" --stage clip)" "done"
check "clip/yt: window recorded in state.json" \
  "$(state get --run-dir "$RUNS/$run" --key clip)" "00:05:00-01:30:00"
# The run id is what every artifact path derives from, so this is what keeps a
# clipped run's transcript and summary away from a full run's.
case "$run" in
  *_c000500-013000_*) ok "clip/yt: window is in the run id" ;;
  *) bad "clip/yt: run id carries no window: $run" ;;
esac
# The clip is derived data with the download's lifetime: written (mark_done
# refuses a missing artifact, so `clip done` proves it existed) and then swept
# with the download once the summary is out.
[ -f "$RUNS/$run/clip.mp4" ] && bad "clip/yt: clip.mp4 kept after the summary" \
  || ok "clip/yt: clip.mp4 swept after the summary"
check "clip/yt: clip stage marked cleaned" \
  "$(state get --run-dir "$RUNS/$run" --key stages.clip.cleaned)" "True"
[ -f "$RUNS/$run/clip.part.mp4" ] && bad "clip/yt: left a .part behind" \
  || ok "clip/yt: no .part left behind"
grep -qx -- "-ss" "$STUB_FFMPEG_ARGS" && ok "clip/yt: ffmpeg seeked" \
  || bad "clip/yt: ffmpeg got no -ss"
grep -qx -- "300.000" "$STUB_FFMPEG_ARGS" && ok "clip/yt: seeked to the start" \
  || bad "clip/yt: wrong start"
grep -qx -- "5100.000" "$STUB_FFMPEG_ARGS" && ok "clip/yt: cut the right duration" \
  || bad "clip/yt: wrong duration"

echo "--- The clip, not the source, is what the later stages are given"
grep -qx -- "$RUNS/$run/clip.mp4" "$STUB_SUMMARIZE_ARGS" \
  && ok "clip: summarize got the clip" || bad "clip: summarize got the source"
grep -qx -- "--clip" "$STUB_SUMMARIZE_ARGS" \
  && ok "clip: window reaches the document provenance" \
  || bad "clip: window never reached summarize"

echo "--- YouTube captions are windowed by transcribe.sh, not by ffmpeg"
# There is no media to cut on the caption path: the transcript comes back whole
# and free, so the same window is applied to the segments instead.
grep -qx -- "--clip-captions" "$STUB_TRANSCRIBE_ARGS" \
  && ok "clip/yt: transcribe told to window the captions" \
  || bad "clip/yt: transcribe never told about the window"

echo "--- A second window on the same input is a second run, not a resume"
out=$(pipeline "https://www.youtube.com/watch?v=clip000000001" \
      --clip 01:30:00-02:00:00 2>&1)
check "clip: exits 0" "$?" "0"
run2=$(latest_run)
[ "$run2" != "$run" ] && ok "clip: a different window makes a different run" \
  || bad "clip: the second window resumed the first window's run"
[ -f "$SUMMARIES_DIR/$run.md" ] && [ -f "$SUMMARIES_DIR/$run2.md" ] \
  && ok "clip: both windows kept their own summary" \
  || bad "clip: one window overwrote the other's summary"

echo "--- The same window, spelled differently, resumes rather than duplicating"
touch "$STUB_FAIL_SUMMARIZE"
pipeline "https://www.youtube.com/watch?v=clip000000002" --clip 00:05:00-01:30:00 \
  >/dev/null 2>&1
first=$(latest_run)
rm -f "$STUB_FAIL_SUMMARIZE"
out=$(pipeline "https://www.youtube.com/watch?v=clip000000002" --clip 5:00-90:00 2>&1)
check "clip: exits 0" "$?" "0"
check "clip: 5:00-90:00 resumed 00:05:00-01:30:00" "$(latest_run)" "$first"

echo "--- An unclipped run of the same input is untouched by a clipped one"
out=$(pipeline "https://www.youtube.com/watch?v=clip000000003" 2>&1)
rc=$?
full=$(latest_run)
check "clip: full run exits 0" "$rc" "0"
out=$(pipeline "https://www.youtube.com/watch?v=clip000000003" --clip 00:05:00- 2>&1)
rc=$?
check "clip: clipped run exits 0" "$rc" "0"
clipped=$(latest_run)
[ "$clipped" != "$full" ] && ok "clip: clipped run is separate from the full run" \
  || bad "clip: the clip resumed the finished full run"
[ -f "$SUMMARIES_DIR/$full.md" ] && ok "clip: the full summary survived" \
  || bad "clip: the full summary was overwritten"
case "$clipped" in
  *_c000500-end_*) ok "clip: an open end is named in the run id" ;;
  *) bad "clip: open-end run id is wrong: $clipped" ;;
esac

echo "--- Kaltura: the cut happens before BOTH branches"
# Kaltura's transcribe branch reads the media (the entry usually has no
# captions), so a cut that ran inside the frames branch would race it and
# AssemblyAI would be billed for the whole lecture.
rm -f "$STUB_FFMPEG_ARGS"
out=$(pipeline "https://cdnapisec.kaltura.com/p/2910381/embedPlaykitJs/uiconf_id/1?entry_id=1_clipclip" \
      --clip 00:10:00-00:20:00 2>&1)
check "clip/kal: exits 0" "$?" "0"
run=$(latest_run)
check "clip/kal: clip stage done" "$(state status --run-dir "$RUNS/$run" --stage clip)" "done"
grep -qx -- "$RUNS/$run/clip.mp4" "$STUB_TRANSCRIBE_ARGS" \
  && ok "clip/kal: transcribe got the clip, not the full download" \
  || bad "clip/kal: transcribe got the uncut media"

echo "--- A local file is cut before it is transcribed"
rm -f "$STUB_FFMPEG_ARGS"
echo "fake recording" > "$TESTROOT/lecture clip.mp4"
out=$(pipeline "$TESTROOT/lecture clip.mp4" --clip 00:01:00-00:02:00 2>&1)
check "clip/local: exits 0" "$?" "0"
run=$(latest_run)
grep -qx -- "$RUNS/$run/clip.mp4" "$STUB_TRANSCRIBE_ARGS" \
  && ok "clip/local: transcribe got the clip" \
  || bad "clip/local: transcribe got the whole file"

echo "--- A bad window fails before anything is downloaded"
before=$(latest_run)
out=$(pipeline "https://www.youtube.com/watch?v=clipbad000001" --clip 01:30:00-00:05:00 2>&1)
check "clip: a backwards window exits 1" "$?" "1"
echo "$out" | grep -q "ends at or before it starts" \
  && ok "clip: says what is wrong with the window" || bad "clip: unclear error"
check "clip: no run was created" "$(latest_run)" "$before"

echo "--- --clip is refused for a live meeting"
before=$(latest_run)
out=$(pipeline "https://meet.google.com/abc-defg-hij" --clip 00:05:00-01:30:00 2>&1)
check "clip/meet: exits 1" "$?" "1"
echo "$out" | grep -q "does not apply to a live meeting" \
  && ok "clip/meet: says why" || bad "clip/meet: unclear error"
check "clip/meet: no run was created" "$(latest_run)" "$before"

echo "--- A failed cut is resumable, and the resume re-cuts"
touch "$STUB_FAIL_CLIP"
pipeline "https://www.youtube.com/watch?v=clipfail00001" --clip 00:05:00-01:30:00 \
  >/dev/null 2>&1
run=$(latest_run)
check "clip: the run failed" "$(state status --run-dir "$RUNS/$run" --stage clip)" "failed"
check "clip: the download was kept" \
  "$(state status --run-dir "$RUNS/$run" --stage fetch_video)" "done"
rm -f "$STUB_FAIL_CLIP"
out=$(pipeline --run-id "$run" 2>&1)
check "clip: the resume exits 0" "$?" "0"
check "clip: the cut succeeded on the resume" \
  "$(state status --run-dir "$RUNS/$run" --stage clip)" "done"

echo "--- A swept clip is cut again when a resume needs the frames back"
# Simulates a summary that has to be redone after the sweep: reset summarize
# and frames, keep fetch_video and clip at their swept `done`.
state reset --run-dir "$RUNS/$run" --stage summarize
state reset --run-dir "$RUNS/$run" --stage frames
out=$(pipeline --run-id "$run" 2>&1)
check "clip re-cut: exits 0" "$?" "0"
echo "$out" | grep -q "\[fetch_video\] swept after the last summary" \
  && ok "clip re-cut: download fetched again" || bad "clip re-cut: no re-download"
echo "$out" | grep -q "\[clip\] swept after the last summary" \
  && ok "clip re-cut: window cut again" || bad "clip re-cut: clip not re-cut"
grep -qx -- "-ss" "$STUB_FFMPEG_ARGS" && ok "clip re-cut: ffmpeg ran" || bad "clip re-cut: ffmpeg did not run"
check "clip re-cut: summarized again" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"
[ -e "$RUNS/$run/clip.mp4" ] && bad "clip re-cut: clip kept afterwards" || ok "clip re-cut: clip swept again"

echo "--- Without --clip nothing changes"
rm -f "$STUB_FFMPEG_ARGS"
out=$(pipeline "https://www.youtube.com/watch?v=noclip0000001" 2>&1)
check "noclip: exits 0" "$?" "0"
run=$(latest_run)
check "noclip: clip stage never runs" \
  "$(state status --run-dir "$RUNS/$run" --stage clip)" "pending"
[ -f "$RUNS/$run/clip.mp4" ] && bad "noclip: wrote a clip anyway" \
  || ok "noclip: no clip.mp4"
[ -f "$STUB_FFMPEG_ARGS" ] && bad "noclip: ran ffmpeg anyway" \
  || ok "noclip: ffmpeg never invoked"
grep -qx -- "--clip" "$STUB_SUMMARIZE_ARGS" \
  && bad "noclip: passed --clip to summarize" \
  || ok "noclip: summarize got no --clip"
grep -qx -- "--clip-captions" "$STUB_TRANSCRIBE_ARGS" \
  && bad "noclip: passed --clip-captions to transcribe" \
  || ok "noclip: transcribe got no window"


echo ""
echo "=================================================================="
echo "N+1. #t= — a window on ONE input, in a multi-input invocation"
echo "=================================================================="

echo "--- Only the input carrying #t= is clipped"
# The case this exists for: several lectures summarized together, some of them
# needing trimming, and one --combine document over the lot. A global --clip
# cannot express it, and splitting into one invocation per window would split
# the combined document too.
rm -f "$STUB_FFMPEG_ARGS"
out=$(pipeline "https://www.youtube.com/watch?v=tsuffix00001" \
               "https://www.youtube.com/watch?v=tsuffix00002#t=00:00:00-01:16:04" \
               "https://www.youtube.com/watch?v=tsuffix00003" --jobs 1 2>&1)
check "t=: exits 0" "$?" "0"
clipped_run=$(ls -1d "$RUNS"/yt_tsuffix00002_* | head -n 1)
plain_run=$(ls -1d "$RUNS"/yt_tsuffix00001_* | head -n 1)
check "t=: the marked input is clipped" \
  "$(state get --run-dir "$clipped_run" --key clip)" "00:00:00-01:16:04"
state get --run-dir "$plain_run" --key clip >/dev/null 2>&1 \
  && bad "t=: the window leaked onto a neighbouring input" \
  || ok "t=: its neighbours are untouched"
case "$(basename "$clipped_run")" in
  yt_tsuffix00002_c000000-011604_*) ok "t=: window is in that run's id" ;;
  *) bad "t=: run id is wrong: $(basename "$clipped_run")" ;;
esac

echo "--- The suffix does not reach the input, the run id, or the document"
# The input string is the auto-resume key and the provenance link. A window
# left inside it would make the link wrong and every resume miss.
check "t=: the suffix is stripped from the stored input" \
  "$(state get --run-dir "$clipped_run" --key input)" \
  "https://www.youtube.com/watch?v=tsuffix00002"
case "$(basename "$clipped_run")" in
  *"#t="*) bad "t=: the suffix leaked into the run id" ;;
  *) ok "t=: no suffix in the run id" ;;
esac

echo "--- #t= overrides --clip for its own input only"
rm -f "$STUB_FFMPEG_ARGS"
out=$(pipeline "https://www.youtube.com/watch?v=tsuffix00004" \
               "https://www.youtube.com/watch?v=tsuffix00005#t=00:10:00-00:20:00" \
               --clip 00:05:00-01:30:00 --jobs 1 2>&1)
check "t=: exits 0" "$?" "0"
check "t=: the unmarked input took --clip" \
  "$(state get --run-dir "$(ls -1d "$RUNS"/yt_tsuffix00004_* | head -n 1)" --key clip)" \
  "00:05:00-01:30:00"
check "t=: the marked input overrode it" \
  "$(state get --run-dir "$(ls -1d "$RUNS"/yt_tsuffix00005_* | head -n 1)" --key clip)" \
  "00:10:00-00:20:00"

echo "--- A Kaltura <iframe> takes the suffix after the closing tag"
# The blob is what gets pasted out of the LMS, so the suffix has to survive
# sitting on the end of 900 characters of HTML with a quoted src in the middle.
IFRAME='<iframe id="kaltura_player" src="https://cdnapisec.kaltura.com/p/2910381/embedPlaykitJs/uiconf_id/52668182?iframeembed=true&amp;entry_id=1_tsuffix1" style="width: 400px;height: 285px;border: 0;" allowfullscreen title="2110423 Online Lecture Sessions"></iframe>'
out=$(pipeline "${IFRAME}#t=00:00:00-00:23:00" 2>&1)
check "t=/kaltura: exits 0" "$?" "0"
run=$(latest_run)
check "t=/kaltura: window recorded" \
  "$(state get --run-dir "$RUNS/$run" --key clip)" "00:00:00-00:23:00"
case "$run" in
  kal_1_tsuffix1_c000000-002300_*) ok "t=/kaltura: entry id and window in the run id" ;;
  *) bad "t=/kaltura: run id is wrong: $run" ;;
esac
# The blob is normalised to a canonical embed URL before summarize sees it;
# what matters here is that no fragment rode along with it.
grep -q -- "#t=" "$STUB_SUMMARIZE_ARGS" \
  && bad "t=/kaltura: the suffix reached summarize" \
  || ok "t=/kaltura: no suffix downstream"

echo "--- A local file takes it too"
echo "fake recording" > "$TESTROOT/suffix lecture.mp4"
out=$(pipeline "$TESTROOT/suffix lecture.mp4#t=00:01:00-00:02:00" 2>&1)
check "t=/local: exits 0" "$?" "0"
run=$(latest_run)
check "t=/local: window recorded" \
  "$(state get --run-dir "$RUNS/$run" --key clip)" "00:01:00-00:02:00"
check "t=/local: the path is intact, suffix and space and all" \
  "$(state get --run-dir "$RUNS/$run" --key input)" "$TESTROOT/suffix lecture.mp4"

echo "--- The same window resumes whether it came from #t= or --clip"
touch "$STUB_FAIL_SUMMARIZE"
pipeline "https://www.youtube.com/watch?v=tsuffix00006#t=00:05:00-01:30:00" \
  >/dev/null 2>&1
first=$(latest_run)
rm -f "$STUB_FAIL_SUMMARIZE"
out=$(pipeline "https://www.youtube.com/watch?v=tsuffix00006" --clip 5:00-90:00 2>&1)
check "t=: exits 0" "$?" "0"
check "t=: the two spellings are one run" "$(latest_run)" "$first"

echo "--- An unparseable window is refused before anything is created"
before=$(latest_run)
out=$(pipeline "https://www.youtube.com/watch?v=tsuffix00007#t=90:00-5:00" 2>&1)
check "t=: exits 1" "$?" "1"
echo "$out" | grep -q "ends at or before it starts" \
  && ok "t=: names the problem" || bad "t=: unclear error"
check "t=: no run created" "$(latest_run)" "$before"

echo "--- A fragment that is not a window is left on the URL"
# "#t=" only becomes a window when what is left of it still looks like an
# input AND the remainder parses. Anything else stays part of the URL rather
# than being silently truncated.
out=$(pipeline "https://www.youtube.com/watch?v=tsuffix00008" 2>&1)
check "t=: a plain URL is unaffected" "$?" "0"
state get --run-dir "$RUNS/$(latest_run)" --key clip >/dev/null 2>&1 \
  && bad "t=: invented a window" || ok "t=: no window on a plain URL"

echo "--- --from-file carries #t=, and still honours real comments"
cat > "$TESTROOT/links.txt" <<'LINKS'
# a comment line
https://www.youtube.com/watch?v=tsuffix00009#t=00:05:00-01:30:00
https://www.youtube.com/watch?v=tsuffix00010   # trailing comment
LINKS
out=$(pipeline --from-file "$TESTROOT/links.txt" --jobs 1 2>&1)
check "t=/from-file: exits 0" "$?" "0"
check "t=/from-file: the window survived the comment stripper" \
  "$(state get --run-dir "$(ls -1d "$RUNS"/yt_tsuffix00009_* | head -n 1)" --key clip)" \
  "00:05:00-01:30:00"
check "t=/from-file: a trailing comment is still stripped" \
  "$(state get --run-dir "$(ls -1d "$RUNS"/yt_tsuffix00010_* | head -n 1)" --key input)" \
  "https://www.youtube.com/watch?v=tsuffix00010"

echo "--- Several windows in one invocation still make one combined document"
COMBINED="$TESTROOT/combined clips.md"
out=$(pipeline "https://www.youtube.com/watch?v=tcomb00000001#t=00:00:00-01:16:04" \
               "https://www.youtube.com/watch?v=tcomb00000002" \
               "https://www.youtube.com/watch?v=tcomb00000003#t=00:00:00-00:23:00" \
               --jobs 1 --combine "$COMBINED" 2>&1)
check "t=/combine: exits 0" "$?" "0"
[ -f "$COMBINED" ] && ok "t=/combine: one document for three windows" \
  || bad "t=/combine: no combined document"
check "t=/combine: all three videos in the one summary" \
  "$(grep -c "^video [0-9]: " "$COMBINED")" "3"
# The clip label travels into parts.json, so the document can say which
# video was a window and summarize.py can label it.
tparts="$(ls -d "$RUNS"/combine_3x_* | while read -r d; do
  [ "$(state get --run-dir "$d" --key output_md)" = "$COMBINED" ] && echo "$d/parts.json"; done | head -n 1)"
check "t=/combine: clip labels reach parts.json" \
  "$(python3 -c 'import json,sys; print(" ".join(str(p["clip"]) for p in json.load(open(sys.argv[1]))["parts"]))' "$tparts")" \
  "00:00:00-01:16:04 None 00:00:00-00:23:00"

echo ""
echo "=================================================================="
echo "Result: $PASS passed, $FAIL failed"
echo "=================================================================="
[ "$FAIL" -eq 0 ] || exit 1
