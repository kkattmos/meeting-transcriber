#!/usr/bin/env bash
# Measure what each local stage of the pipeline costs on THIS machine.
#
#   ./benchmark.sh                  120s synthetic recording, every stage
#   ./benchmark.sh --seconds 300    a longer one (steadier numbers)
#   ./benchmark.sh --quick          30s, for a first look
#   ./benchmark.sh --browser        also: Firefox ESR on a hidden Xvfb display
#                                   playing a full-screen 720p VP8 video — a
#                                   stand-in for the browser rendering a call
#   ./benchmark.sh --watch-run ID [--seconds N]
#                                   sample a recording IN PROGRESS: CPU and
#                                   memory of its Xvfb, browser, ffmpeg and
#                                   driver, per process group, for N seconds
#
# Needs ffmpeg/ffprobe and python3. The Python stages (frame crop/hash, the
# silence check, the PDF) use the project venv ($MEETING_BOT_VENV or .venv)
# and are skipped with a note when it is missing. Nothing touches
# $MEETING_BOT_ROOT, the output directories or any API: remote stages
# (AssemblyAI, Claude, Gemini, downloads) are not measurable here — their
# cost is quota, and summarize's lands in state.json's `usage`.
#
# Every number is for the settings the recorder and extract_frames.py really
# use; if you change those, change the commands below too.
#
# Options: --seconds N, --quick, --keep (keep the work dir), --workdir DIR,
#          --json FILE (machine-readable results), --no-python,
#          --browser, --watch-run ID, --watch-pid PID.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SECONDS_MEDIA=120
KEEP=0
WORKDIR=""
JSON_OUT=""
USE_PY=1
WATCH_RUN=""
WATCH_PID=""
SECONDS_GIVEN=0
BROWSER=0

usage() { sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; }

need_value() {
  if [ $# -lt 2 ] || [ -z "$2" ] || [ "${2#--}" != "$2" ]; then
    echo "benchmark.sh: $1 needs a value" >&2; exit 2
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --seconds) need_value "$@"; SECONDS_MEDIA="$2"; SECONDS_GIVEN=1; shift 2 ;;
    --quick) SECONDS_MEDIA=30; shift ;;
    --keep) KEEP=1; shift ;;
    --workdir) need_value "$@"; WORKDIR="$2"; shift 2 ;;
    --json) need_value "$@"; JSON_OUT="$2"; shift 2 ;;
    --no-python) USE_PY=0; shift ;;
    --browser) BROWSER=1; shift ;;
    --watch-run) need_value "$@"; WATCH_RUN="$2"; shift 2 ;;
    --watch-pid) need_value "$@"; WATCH_PID="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "benchmark.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
  esac
done
case "$SECONDS_MEDIA" in ''|*[!0-9]*|0) echo "--seconds must be a positive integer" >&2; exit 2 ;; esac

# The process-tree sampler behind --watch-* and --browser. Roots are a
# comma-separated pid list; each root's whole tree is counted.
write_watch_py() {
  cat > "$1" <<'PY'
import os, sys, time
roots, span = [int(p) for p in sys.argv[1].split(",")], float(sys.argv[2])
tick = os.sysconf("SC_CLK_TCK")
page = os.sysconf("SC_PAGE_SIZE")
ncpu = os.cpu_count() or 1

def snapshot():
    procs = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                s = f.read()
            with open(f"/proc/{d}/statm") as f:
                rss = int(f.read().split()[1]) * page
        except OSError:
            continue
        comm = s[s.index("(") + 1:s.rindex(")")]
        rest = s[s.rindex(")") + 2:].split()
        procs[int(d)] = (int(rest[1]), comm, int(rest[11]) + int(rest[12]), rss)
    return procs

def tree(procs):
    kids, out, todo = {}, set(), list(roots)
    for pid, (ppid, *_r) in procs.items():
        kids.setdefault(ppid, []).append(pid)
    while todo:
        p = todo.pop()
        if p in procs and p not in out:
            out.add(p)
            todo.extend(kids.get(p, []))
    return out

def group(comm):
    c = comm.lower()
    for key, name in (("xvfb", "Xvfb (virtual display)"),
                      ("ffmpeg", "ffmpeg (x264 encode + audio)"),
                      ("geckodriver", "geckodriver"), ("chrome", "browser"),
                      ("firefox", "browser"), ("web content", "browser"),
                      ("isolated", "browser"), ("content", "browser"), ("webextensions", "browser"),
                      ("socket process", "browser"), ("rdd process", "browser"),
                      ("utility process", "browser"), ("privileged", "browser"),
                      ("python", "capture.py / watchers"),
                      ("pactl", "audio watch"), ("sleep", "audio watch"),
                      ("bash", "record_screen.sh")):
        if key in c:
            return name
    return comm

a = snapshot()
if not any(r in a for r in roots):
    sys.exit(f"pid {sys.argv[1]} is not running")
t0 = time.monotonic()
time.sleep(span)
b = snapshot()
dt = time.monotonic() - t0
alive = tree(b)
rows = {}
for pid in alive:
    _pp, comm, cpu, rss = b[pid]
    base = a[pid][2] if pid in a and a[pid][1] == comm else 0
    g = rows.setdefault(group(comm), [0.0, 0, 0])
    g[0] += (cpu - base) / tick
    g[1] += rss
    g[2] += 1
print(f"Process tree of pid {sys.argv[1]}, sampled over {dt:.0f}s on {ncpu} threads\n")
print(f"{'component':32} {'procs':>5} {'cores':>6} {'% box':>6} {'RSS MB':>7}")
tot = [0.0, 0]
for name, (cpu, rss, n) in sorted(rows.items(), key=lambda kv: -kv[1][0]):
    print(f"{name:32} {n:5d} {cpu/dt:6.2f} {100*cpu/dt/ncpu:5.1f}% {rss/2**20:7.0f}")
    tot[0] += cpu; tot[1] += rss
print(f"{'TOTAL':32} {'':5} {tot[0]/dt:6.2f} {100*tot[0]/dt/ncpu:5.1f}% {tot[1]/2**20:7.0f}")
print("\n'cores' = CPU-seconds per second (1.00 = one thread busy). RSS double-"
      "counts pages Firefox's processes share.")
PY
}

# ── Watch mode: a live recording's process tree ───────────────────────────
if [ -n "$WATCH_RUN$WATCH_PID" ]; then
  if [ -n "$WATCH_RUN" ]; then
    # shellcheck source=/dev/null
    [ -f "$SCRIPT_DIR/source_env.sh" ] && . "$SCRIPT_DIR/source_env.sh" >/dev/null 2>&1
    ROOT="${MEETING_BOT_ROOT:-$HOME/.local/share/meeting-bot}"
    PIDF="$ROOT/runs/$WATCH_RUN/record.pid"
    [ -f "$PIDF" ] || { echo "no $PIDF — is that run recording?" >&2; exit 1; }
    WATCH_PID="$(sed -n 's/^record=//p' "$PIDF")"
  fi
  [ "$SECONDS_GIVEN" -eq 1 ] || SECONDS_MEDIA=60
  WATCH_PY="$(mktemp "${TMPDIR:-/tmp}/meeting-bot-watch.XXXXXX")"
  trap 'rm -f "$WATCH_PY"' EXIT
  write_watch_py "$WATCH_PY"
  python3 "$WATCH_PY" "$WATCH_PID" "$SECONDS_MEDIA"
  exit $?
fi

# ── Batch mode: synthetic media through every local stage ─────────────────
for t in ffmpeg ffprobe python3; do
  command -v "$t" >/dev/null || { echo "benchmark.sh: $t not found" >&2; exit 1; }
done

VENV_PY="${MEETING_BOT_VENV:-$SCRIPT_DIR/.venv}/bin/python3"
if [ "$USE_PY" -eq 1 ] && ! "$VENV_PY" -c 'import PIL' 2>/dev/null; then
  echo "note: no project venv with Pillow at $VENV_PY — Python stages skipped" >&2
  USE_PY=0
fi

if [ -z "$WORKDIR" ]; then
  WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/meeting-bot-bench.XXXXXX")"
else
  mkdir -p "$WORKDIR"
fi
cleanup() { [ "$KEEP" -eq 1 ] || rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT
RESULTS="$WORKDIR/results.tsv"
: > "$RESULTS"

GEOMETRY="${RECORD_GEOMETRY:-1920x1080}"
FPS="${RECORD_FRAMERATE:-15}"
PERIOD="${FRAME_PERIOD_SECONDS:-60}"
THRESH="${SCENE_THRESHOLD:-0.3}"

# measure NAME MEDIA_SECONDS CMD... — wall, children's CPU, peak RSS.
cat > "$WORKDIR/measure.py" <<'PY'
import os, resource, subprocess, sys, time
name, media, out, cmd = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4:]
t0 = time.monotonic()
rc = subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
wall = time.monotonic() - t0
ru = resource.getrusage(resource.RUSAGE_CHILDREN)
cpu = ru.ru_utime + ru.ru_stime
with open(out, "a") as f:
    f.write(f"{name}\t{media}\t{wall:.3f}\t{cpu:.3f}\t{ru.ru_maxrss/1024:.0f}\t{rc}\n")
sys.exit(rc)
PY
measure() { python3 "$WORKDIR/measure.py" "$1" "$2" "$RESULTS" "${@:3}"; }

echo "==> $(lscpu 2>/dev/null | sed -n 's/^Model name: *//p' | head -1), $(nproc) threads, $(free -g | awk '/^Mem:/{print $2}')GB RAM"
echo "==> ffmpeg $(ffmpeg -version | head -1 | cut -d' ' -f3); ${SECONDS_MEDIA}s of ${GEOMETRY}@${FPS}fps in $WORKDIR"
echo ""

# Two sources, because x264's cost follows the picture:
#   slides — a static slide that changes five times (a change the scene pass
#            must catch) with a moving 480x270 camera tile in the corner;
#   camera — a full-screen moving picture with sensor-like noise, the
#            Spotlight layout on a speaker with their camera on.
# Generating a source costs something too; each `source_*` row measures that
# alone, and the `record_encode_*` rows are reported with it subtracted.
SLIDE_EVERY=$(( SECONDS_MEDIA / 6 > 5 ? SECONDS_MEDIA / 6 : 5 ))
SRC="smptehdbars=s=${GEOMETRY}:r=${FPS}[bg];yuvtestsrc=s=${GEOMETRY}:r=${FPS}[alt];[bg][alt]overlay=enable='mod(floor(t/${SLIDE_EVERY})\\,2)'[s];testsrc2=s=480x270:r=${FPS}[cam];[s][cam]overlay=W-w-40:H-h-40"
CAM="testsrc2=s=${GEOMETRY}:r=${FPS},noise=alls=12:allf=t"
AUD="sine=f=440:sample_rate=48000"
ENC=(-c:v libx264 -preset ultrafast -crf 28 -c:a aac -b:a 128k -pix_fmt yuv420p)

echo "[1/8] recording encode, slides — record_screen.sh's ffmpeg settings"
measure source_slides "$SECONDS_MEDIA" ffmpeg -nostdin -y -f lavfi -i "$SRC" -f lavfi -i "$AUD" \
  -t "$SECONDS_MEDIA" -f null -
measure record_encode_slides_raw "$SECONDS_MEDIA" ffmpeg -nostdin -y \
  -f lavfi -i "$SRC" -f lavfi -i "$AUD" -t "$SECONDS_MEDIA" "${ENC[@]}" "$WORKDIR/rec.mp4"
REC="$WORKDIR/rec.mp4"
[ -s "$REC" ] || { echo "encode failed — see ffmpeg" >&2; exit 1; }

echo "[2/8] recording encode, full-screen camera"
measure source_camera "$SECONDS_MEDIA" ffmpeg -nostdin -y -f lavfi -i "$CAM" -f lavfi -i "$AUD" \
  -t "$SECONDS_MEDIA" -f null -
measure record_encode_camera_raw "$SECONDS_MEDIA" ffmpeg -nostdin -y \
  -f lavfi -i "$CAM" -f lavfi -i "$AUD" -t "$SECONDS_MEDIA" "${ENC[@]}" "$WORKDIR/rec_cam.mp4"

echo "[3/8] frames: scene-change pass (threshold $THRESH) — extract_frames.py"
mkdir -p "$WORKDIR/frames"
measure frames_scene "$SECONDS_MEDIA" ffmpeg -nostdin -i "$REC" \
  -vf "select='gt(scene,$THRESH)',showinfo" -vsync vfr "$WORKDIR/frames/scene_%05d.jpg"

echo "[4/8] frames: periodic pass (every ${PERIOD}s)"
measure frames_periodic "$SECONDS_MEDIA" ffmpeg -nostdin -i "$REC" \
  -vf "fps=1/$PERIOD,showinfo" -vsync vfr "$WORKDIR/frames/periodic_%05d.jpg"

echo "[5/8] --clip: stream copy vs CLIP_REENCODE=1 — lib/clip.py"
measure clip_copy "$SECONDS_MEDIA" env CLIP_REENCODE=0 \
  python3 "$SCRIPT_DIR/lib/clip.py" cut "$REC" "$WORKDIR/clip_copy.mp4" "0:05-end"
measure clip_reencode "$SECONDS_MEDIA" env CLIP_REENCODE=1 \
  python3 "$SCRIPT_DIR/lib/clip.py" cut "$REC" "$WORKDIR/clip_re.mp4" "0:05-end"

if [ "$USE_PY" -eq 1 ]; then
  echo "[6/8] silence check before upload — lib/audiocheck.py"
  measure audiocheck "$SECONDS_MEDIA" "$VENV_PY" "$SCRIPT_DIR/lib/audiocheck.py" "$REC"

  echo "[7/8] frame prep for the model: blank test, texture hash, crop+downscale"
  cat > "$WORKDIR/frameprep.py" <<'PY'
import glob, os, sys
sys.path.insert(0, sys.argv[1])
import framecrop
out = sys.argv[3]
os.makedirs(out, exist_ok=True)
for p in sorted(glob.glob(os.path.join(sys.argv[2], "*.jpg"))):
    framecrop.is_blank(p)
    framecrop.frame_hash(p)
    framecrop.fit_for_llm(p, os.path.join(out, os.path.basename(p)), max_dim=768)
PY
  NFRAMES=$(ls "$WORKDIR/frames"/*.jpg 2>/dev/null | wc -l)
  measure "frame_prep(${NFRAMES}f)" "$SECONDS_MEDIA" "$VENV_PY" "$WORKDIR/frameprep.py" \
    "$SCRIPT_DIR/summarize" "$WORKDIR/frames" "$WORKDIR/llm"

  echo "[8/8] PDF render (WeasyPrint + mathtext) — summarize/pdf.py, maths on and off"
  {
    printf '<!-- meeting-transcriber\nlanguage: en\n-->\n# Benchmark Sheet\n\n'
    for i in $(seq 1 12); do
      cat <<MD
## Section $i: Signals and systems

> [!CONCEPT] Convolution
> The output is \$y(t) = \\int_{-\\infty}^{\\infty} x(\\tau)\\,h(t-\\tau)\\,d\\tau\$.

A piecewise definition, as a lecture writes them:

\$\$
u(t) = \\begin{cases} 1 & t \\ge 0 \\\\ 0 & t < 0 \\end{cases}
\\qquad
A = \\begin{bmatrix} a_{11} & a_{12} \\\\ a_{21} & a_{22} \\end{bmatrix}
\\qquad
\\frac{d}{dt}e^{j\\omega_$i t} = j\\omega_$i e^{j\\omega_$i t}
\$\$

- Linearity: \$a x_1 + b x_2 \\mapsto a y_1 + b y_2\$ for section $i
    - Time invariance: \$x(t-t_0) \\mapsto y(t-t_0)\$

| Property | Continuous | Discrete |
|---|---|---|
| Energy | \$\\int |x|^2 dt\$ | \$\\sum |x[n]|^2\$ |

\`\`\`python
def convolve(x, h):
    return [sum(x[k] * h[n - k] for k in range(len(x)) if 0 <= n - k < len(h))
            for n in range(len(x) + len(h) - 1)]
\`\`\`

MD
    done
  } > "$WORKDIR/sheet.md"
  measure pdf_render "$SECONDS_MEDIA" env SUMMARY_LANGUAGE=en PDF_MATH=1 \
    "$VENV_PY" "$SCRIPT_DIR/summarize/pdf.py" "$WORKDIR/sheet.md" "$WORKDIR/sheet.pdf"
  measure pdf_render_nomath "$SECONDS_MEDIA" env SUMMARY_LANGUAGE=en PDF_MATH=0 \
    "$VENV_PY" "$SCRIPT_DIR/summarize/pdf.py" "$WORKDIR/sheet.md" "$WORKDIR/sheet_nomath.pdf"
else
  echo "[6-8/8] skipped (no venv)"
fi

BROWSER_REPORT=""
if [ "$BROWSER" -eq 1 ]; then
  FF="${FIREFOX_BIN:-firefox-esr}"
  if ! command -v Xvfb >/dev/null || ! command -v "$FF" >/dev/null; then
    echo "[browser] skipped: needs Xvfb and $FF"
  else
    echo "[browser] $FF on a hidden display, full-screen 720p30 VP8, sampled ${SECONDS_MEDIA}s"
    # Meet sends VP8/VP9/AV1 at up to 720p; the browser decodes it and
    # composites the page in software (Xvfb has no GPU), as in a real call.
    # What this leaves out — WebRTC, Meet's own JavaScript, several tiles —
    # only adds, so treat the result as a floor.
    ffmpeg -nostdin -y -loglevel error -f lavfi \
      -i "testsrc2=s=1280x720:r=30,noise=alls=12:allf=t" -t 60 \
      -c:v libvpx -b:v 1500k -deadline realtime -cpu-used 8 "$WORKDIR/cam.webm"
    printf '%s\n' '<html><body style="margin:0;background:#000"><video src="cam.webm" autoplay muted loop style="width:100vw;height:100vh;object-fit:cover"></video></body></html>' \
      > "$WORKDIR/play.html"
    mkdir -p "$WORKDIR/ffprofile"
    printf '%s\n' 'user_pref("media.autoplay.default", 0);' \
      'user_pref("browser.shell.checkDefaultBrowser", false);' \
      'user_pref("datareporting.policy.dataSubmissionEnabled", false);' \
      'user_pref("browser.aboutwelcome.enabled", false);' > "$WORKDIR/ffprofile/user.js"
    # shellcheck source=lib/xsession.sh
    . "$SCRIPT_DIR/lib/xsession.sh"
    XVFB_PID="" XVFB_DISPLAY_NUM=""
    if DNUM="$(xsession_pick_display)" && xsession_start_xvfb "$DNUM" "$GEOMETRY"; then
      ( unset WAYLAND_DISPLAY XDG_SESSION_TYPE
        export DISPLAY=":$DNUM" GDK_BACKEND=x11 MOZ_ENABLE_WAYLAND=0
        exec "$FF" --no-remote -profile "$WORKDIR/ffprofile" --kiosk \
          --width "${GEOMETRY%x*}" --height "${GEOMETRY#*x}" "file://$WORKDIR/play.html"
      ) >/dev/null 2>&1 &
      FF_PID=$!
      sleep 12   # geckodriver-free, but the window still takes seconds to map
      write_watch_py "$WORKDIR/watch.py"
      BROWSER_REPORT="$(python3 "$WORKDIR/watch.py" "$FF_PID,$XVFB_PID" "$SECONDS_MEDIA")"
      ffmpeg -nostdin -y -loglevel error -f x11grab -video_size "$GEOMETRY" -i ":$DNUM" \
        -frames:v 1 "$WORKDIR/browser.png" || true
      kill "$FF_PID" 2>/dev/null || true
      sleep 2
      xsession_stop_xvfb
    else
      echo "[browser] could not start Xvfb"
    fi
  fi
fi

echo ""
python3 - "$RESULTS" "$(nproc)" "$JSON_OUT" <<'PY'
import json, sys
path, ncpu, json_out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
rows = []
for line in open(path):
    name, media, wall, cpu, rss, rc = line.rstrip("\n").split("\t")
    rows.append(dict(name=name, media=float(media), wall=float(wall),
                     cpu=float(cpu), rss_mb=int(rss), rc=int(rc)))
by = {r["name"]: r for r in rows}
for kind in ("slides", "camera"):
    s, e = by.get(f"source_{kind}"), by.get(f"record_encode_{kind}_raw")
    if s and e:
        rows.append(dict(name=f"record_encode_{kind}", media=e["media"],
                         wall=max(e["wall"] - s["wall"], 0.001),
                         cpu=max(e["cpu"] - s["cpu"], 0.0),
                         rss_mb=e["rss_mb"], rc=e["rc"], derived=True))
rows.sort(key=lambda r: -r["cpu"] * 3600 / r["media"] if not r["name"].startswith("pdf") else 0)
print(f"{'stage':22} {'wall s':>7} {'CPU s':>7} {'cores':>6} {'x realtime':>10}"
      f" {'CPU min / media hour':>21} {'RSS MB':>7}")
for r in rows:
    if r["name"].startswith("source_") or r["name"].endswith("_raw"):
        continue
    cores = r["cpu"] / r["wall"] if r["wall"] and not r.get("derived") else 0
    xrt = r["media"] / r["wall"] if r["wall"] else 0
    per_hour = r["cpu"] * 3600 / r["media"] / 60
    flag = "" if r["rc"] == 0 else f"  (exit {r['rc']})"
    if r["rc"] == 234 and r["name"].startswith("frames_"):
        flag = "  (no frame emitted — media too short)"
    xs = f"{xrt:9.1f}x" if not r["name"].startswith(("pdf", "frame_prep", "record_encode")) else "        -"
    ph = f"{per_hour:21.1f}" if not r["name"].startswith("pdf") else f"{'(per document)':>21}"
    cs = f"{cores:6.2f}" if not r.get("derived") else f"{'-':>6}"
    print(f"{r['name']:22} {r['wall']:7.1f} {r['cpu']:7.1f} {cs} {xs:>10}"
          f" {ph} {r['rss_mb']:7d}{flag}")
print()
for r in rows:
    if r["name"].startswith("record_encode_") and not r["name"].endswith("_raw"):
        load = r["cpu"] / r["media"]
        print(f"Live, {r['name'][14:]:6}: the encoder alone needs ~{load:.2f} cores "
              f"({100*load/ncpu:.0f}% of {ncpu} threads).")
print("The browser rendering the call comes on top of that — measure it with "
      "--watch-run <id> during a meeting.")
fp = next((r for r in rows if r["name"].startswith("frame_prep")), None)
if fp:
    n = int(fp["name"].split("(")[1].rstrip("f)") or 0)
    if n:
        print(f"Frame prep: {1000*fp['cpu']/n:.0f} ms CPU per frame.")
if json_out:
    with open(json_out, "w") as f:
        json.dump({"threads": ncpu, "rows": rows}, f, indent=2)
    print(f"JSON: {json_out}")
PY
if [ -n "$BROWSER_REPORT" ]; then
  echo ""
  echo "Browser stand-in (a floor for the browser in a real call):"
  printf '%s\n' "$BROWSER_REPORT"
  [ "$KEEP" -eq 1 ] && echo "Screenshot of what it rendered: $WORKDIR/browser.png"
fi
[ "$KEEP" -eq 1 ] && echo "Work dir kept: $WORKDIR"
exit 0
