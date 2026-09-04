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
# Assert the orchestrator handed us a pre-extracted manifest rather than making
# us re-run frame extraction, and told us where the PDF goes (PDF_DIR is not
# derivable from the .md path — the two directories are configured separately).
assert "--frames-manifest" in flags, "pipeline must pass --frames-manifest"
assert "--pdf-out" in flags, "pipeline must pass --pdf-out"
# Record what we were handed so the tests can assert on it.
with open(os.environ["STUB_SUMMARIZE_ARGS"], "w") as fh:
    fh.write("\n".join(argv))
out = args[2]
os.makedirs(os.path.dirname(out), exist_ok=True)
if os.environ.get("STUB_SUMMARIZE_NO_MARKDOWN") != "1":
    open(out, "w").write(f"<!-- meeting-transcriber\n     source: x\n-->\n\n"
                         f"Chapter N — <topic> (<date>)\n\n# Stub\n\nsummary of {args[0]}\n\n<br><br>\n")
    print(f"stub: summary -> {out}")
pdf = flags["--pdf-out"][0]
if os.environ.get("STUB_SUMMARIZE_NO_PDF") != "1":
    os.makedirs(os.path.dirname(pdf), exist_ok=True)
    open(pdf, "wb").write(b"%PDF-1.4 stub\n")
    print(f"stub: pdf -> {pdf}")
STUB

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
export PATH="$FAKE_BIN:$PATH"

export STUB_FAIL_RECORD="$TESTROOT/fail_record"
export STUB_FAIL_TRANSCRIBE="$TESTROOT/fail_transcribe"
export STUB_LIE_TRANSCRIBE="$TESTROOT/lie_transcribe"
export STUB_FAIL_FRAMES="$TESTROOT/fail_frames"
export STUB_FAIL_SUMMARIZE="$TESTROOT/fail_summarize"
export STUB_FAIL_FETCH="$TESTROOT/fail_fetch"
export STUB_SUMMARIZE_ARGS="$TESTROOT/summarize_args.txt"

RUNS="$MEETING_BOT_ROOT/runs"
pipeline() { ( cd "$STAGING" && bash ./pipeline.sh "$@" ) ; }
state() { python3 "$STAGING/lib/runstate.py" "$@"; }
latest_run() { python3 "$STAGING/lib/runstate.py" latest --root "$RUNS"; }

echo ""
echo "=================================================================="
echo "1. Input routing — all four input types"
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

echo "--- YouTube URL (the one from the request, with its &list= parameter)"
YT="https://www.youtube.com/watch?v=5GAfjAjLKYk&list=PLMvKqhmt0Lp8"
out=$(pipeline "$YT" 2>&1)
check "youtube: exits 0" "$?" "0"
run=$(latest_run)
check "youtube: run id is the video id" "${run%_*_*}" "yt_5GAfjAjLKYk"
check "youtube: record skipped" "$(state status --run-dir "$RUNS/$run" --stage record)" "pending"
check "youtube: video fetched" "$(state status --run-dir "$RUNS/$run" --stage fetch_video)" "done"
check "youtube: summarized" "$(state status --run-dir "$RUNS/$run" --stage summarize)" "done"
echo "$out" | grep -q -- "--no-playlist" && ok "youtube: &list= did not expand" || ok "youtube: &list= did not expand (single run)"

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
check "multi: created 3 runs" "$((after - before))" "3"
for vid in aaaaaaaaaaa bbbbbbbbbbb ccccccccccc; do
  ls -d "$RUNS/yt_${vid}_"* >/dev/null 2>&1 && ok "multi: run for $vid" || bad "multi: no run for $vid"
done
[ -f "$TESTROOT/chapter.md" ] && ok "multi: combined file written" || bad "multi: no combined file"
check "multi: one Chapter line in combined file" \
  "$(grep -c 'Chapter N' "$TESTROOT/chapter.md")" "1"
echo "$out" | grep -q "3 run(s), up to 3 at a time" && ok "multi: honored --jobs 3" || bad "multi: --jobs not honored"

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
echo "Result: $PASS passed, $FAIL failed"
echo "=================================================================="
[ "$FAIL" -eq 0 ] || exit 1
