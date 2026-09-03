# CLAUDE.md — Project context for future Claude sessions

This file is the canonical reference for architectural decisions, conventions,
and non-obvious facts about this project. Read it before exploring the source
so you don't re-derive context that's already settled. After every edit, update
it so it stays accurate. **Always ask the user questions first.**

User-facing docs (what this does, how to run it) live in `README.md` — the only
other Markdown file in the repo, deliberately. This file is for the things that
aren't obvious from reading code or git history.

### Note from AssemblyAI
Before writing AssemblyAI code, read https://www.assemblyai.com/docs/agent-instructions.md
and https://www.assemblyai.com/docs/llms.txt. The API has changed — do not rely on
memorized parameter names.

## What this project is, in one paragraph

A meeting/lecture bot for a **Alpine Linux** guest on Proxmox. It joins a Google
Meet or Zoom call in a persistent real-Chrome profile (so Google's sign-in flow
doesn't get blocked by automation-detection heuristics), records both the screen
and the meeting audio into an MP4, transcribes the audio with the AssemblyAI
pre-recorded API (or youtube-transcript.io for YouTube URLs), and produces an AI
summary combining the transcript with keyframes extracted from the recording.
It accepts several inputs per invocation, runs them concurrently, and resumes
anything that failed partway.

## The host/container split — read this first

**The host is Alpine, which is musl. Chrome is glibc-only.** Google ships no
musl build, and Playwright does not support Alpine for its bundled browsers.
The project needs *real* Chrome (see the MUST-NOT-CHANGE list). Those facts are
irreconcilable on one filesystem, so:

- **Alpine host** — Python venv, ffmpeg, yt-dlp, stages 2 and 3
  (transcribe/summarize), the Docker daemon, and all the orchestration.
- **Debian container** (`docker/Dockerfile.recorder`) — Xvfb, PulseAudio, real
  `google-chrome-stable`, Playwright, ffmpeg. Stage 1 and the first-time login.

`docker/recorder_lib.sh` is the shared launcher. The repo is bind-mounted at
`/app` **read-only** (with `PYTHONDONTWRITEBYTECODE=1`, so root-owned `.pyc`
files don't appear in the user's checkout), and `/opt/meeting-bot` is mounted
read-write at the same path inside and out, so every path in state.json means
the same thing on both sides.

**Per-run isolation is a side effect of the container, not of naming.** Display
`:99` and the sink `meeting_sink` stay hardcoded inside the container because
each recording gets its own PID/IPC/network namespace. Do not "fix" this by
allocating display numbers per run — the container boundary already solved it.

## Configuration

Non-secret env vars live in a single `.env` at the repo root; `.env.example` is
the committed template. `.env` is gitignored alongside
`/opt/meeting-bot/secrets/`.

`pipeline.sh`, `lib/run_one.sh`, `transcribe.sh`, `first_time_login.sh` and
`record_screen.sh` all source `source_env.sh`. `summarize.py` carries its own
`_load_dotenv()` for direct invocation. The loader fills in unset values only —
an already-exported var always wins.

YouTube-API keys live in a separate JSON file because they're inherently an
array: `/opt/meeting-bot/secrets/youtube_transcript_keys.json`, overridable via
`YT_TRANSCRIPT_KEYS_FILE`.

## The run model

Everything mutable about a run lives in `/opt/meeting-bot/runs/<run_id>/`:

```
runs/<run_id>/
  state.json      per-stage status + artifact paths   (lib/runstate.py)
  state.lock      flock target for read-modify-write
  run.lock/       mkdir-based single-writer lock + pid
  logs/<stage>.log
  kill            per-run kill sentinel
  admitted        per-run admission marker
  video.mp4       YouTube download, when applicable
```

`run_id` is `<safe_name>_<YYYYmmdd_HHMMSS>`, and **all artifact paths derive
from it** — never from a fresh timestamp. A resumed run must land on the same
filenames the first attempt used or it can't tell what already succeeded. This
is why `transcribe.sh` has `--out-base` and why `pipeline.sh` no longer uses
`ls -1t` globbing to find the previous stage's output.

Stage DAG (`lib/run_one.sh`):

```
record ─┐                     (meeting URLs only)
        ├─> [ transcribe ]  ─┐
input ──┤                    ├─> summarize
        └─> fetch_video ──> frames
```

`transcribe` and `fetch_video`→`frames` run as concurrent bash branches; both
write state through the flock'd `runstate.py`, so their writes can't clobber
each other.

### Resume semantics

- Re-running the same command **resumes by default**: `runstate.py find` looks
  for a run with the same `input` whose `summarize` isn't done.
- A stage counts as done only if **every artifact it recorded still exists**.
  A state file that disagrees with the filesystem is worse than none.
- `run.lock` is a directory containing the owner's pid. A lock whose owner is
  gone is **taken over**, not treated as fatal — a SIGKILL'd run has to stay
  resumable, which is exactly the case resume exists for.
- `--force` resets all stages; `--run-id` / `--resume-last` / `--resume-all`
  are the explicit forms.

## Per-stage reference

### Stage 1 — Recording (`screen/record_screen.sh` → `screen/record_in_container.sh`)

- Host script resolves paths, installs the `Ctrl+\` trap, and runs the container
  in **attach** mode so its exit code propagates to the pipeline.
- In-container script does Xvfb → `audio-setup.sh` → `capture.py` → ffmpeg.
- **Geometry is 1920x1080 everywhere**: the Xvfb head, Chrome's `--kiosk`
  window (`capture.py`), and ffmpeg's `-video_size`. A mismatch produces black
  edges. `--kiosk` alone isn't enough on some Xvfb/Chrome combos, which is why
  `--window-size` is also passed.
- Encoder: `libx264 -preset ultrafast -crf 28`, audio `aac -b:a 128k`.
- Kill: the host touches `runs/<id>/kill` (bind-mounted), `capture.py` sees it
  on the next poll and clicks Leave. `kill_meeting.sh` force-removes the
  container only after a grace period.
- Failure artifacts (`join_failed.png`, `not_admitted.png`) go in the run dir,
  not a shared directory where the next run would overwrite them.

### Stage 2 — Transcribe (`transcribe/transcribe.sh`)

- Local files → AssemblyAI (`assemblyai_client.py`). MP3/MP4/M4A/WAV go
  directly; WEBM/OGG are demuxed to MP3 with ffmpeg first.
- YouTube URLs → youtube-transcript.io (`yt_transcript_client.py`). No audio
  download; captions come back as `{text, offset_ms, duration_ms}` segments.
- Both feed one shared writer producing `.txt` + `.srt`.
- `--out-base PATH` overrides the timestamped default (see the run model).
- Language default `th`, override via arg or `ASSEMBLYAI_LANGUAGE`.

### Stage 3 — Summarize (`summarize/`)

Split across five modules:

- `summarize.py` — entry point and orchestration.
- `llm_client.py` — backend dispatch + the fallback chain.
- `retry.py` — transient-failure policy (503/429/5xx).
- `chunking.py` — splitting long transcripts, assigning frames to chunks.
- `mapreduce.py` — parallel chunk summarization + the merge call.
- `document.py` — the course-note document wrapper and `--combine`.

Backends: `gemini` (default first in the chain), `anthropic`/`fcc`,
`nvidia_nim`, `ollama`. `SUMMARY_BACKEND=fallback` is the default mode and
walks `SUMMARY_FALLBACK_CHAIN` (default `gemini,fcc,nvidia_nim`).

**Retry policy.** Every backend's network call goes through
`retry.with_retries`. Retryable: 408/409/425/429/500/502/503/504, connection and
timeout errors, and provider wording like "overloaded" / "UNAVAILABLE" /
"server is busy" that some SDKs raise without a usable status code. Not
retryable: 400/401/403/404/405/422 — a bad key fails identically forever, and
retrying only delays the fallback to a backend that would have worked. Backoff
is exponential with **full** jitter (not ±10%) specifically because parallel
chunk requests must not retry in lockstep against the server that just said it
was overloaded. `Retry-After` wins when present and ≤ 300s; a longer one means
give up and let the chain advance.

**Chunking.** Above `SUMMARY_CHUNK_CHARS` (24000), the transcript is split and
chunks are summarized concurrently, then merged by one more LLM call using
`prompts/_merge.md`. Chunking prefers the `.srt` sibling of the `.txt`, because
that is the only place segment timestamps live — and timestamps are what let
each chunk carry the frames that were on screen while those words were spoken.
Without an `.srt` it falls back to splitting text and dividing frames
proportionally. A chunk that fails does not discard the others: the merge
proceeds over what succeeded and the document says which parts are missing.

**Frames.** `screen/extract_frames.py` does a scene-change pass
(`SCENE_THRESHOLD`, default 0.3) plus a periodic pass (`FRAME_PERIOD_SECONDS`,
default 30), deduplicated by ±half-period, into `frames/<run_id>/manifest.json`.
The pipeline runs this as its own stage and passes `--frames-manifest` to
`summarize.py`.

## Output document format

`document.py` builds the wrapper **in code**, not via the prompt:

```
<!-- meeting-transcriber ... source / model / prompt / run_id / generated -->
Chapter N — <topic> (<date>)
# <video title from yt-dlp>
Youtube Link: `<url>`
<details><summary> View Transcript </summary>  ...4-space indented...  </details>
<br>
...the model's body...
<br><br>
```

Shaped to match the user's course files (`2_Transcripts/chapter1.md`,
`chapter2.md`) so output drops straight in. Decisions behind it:

- **Code builds the wrapper, the model writes only the body.** The link,
  transcript and provenance can then never be hallucinated or truncated, and an
  ~80KB transcript doesn't round-trip through the model just to be echoed back.
- **Provenance is an HTML comment**, so it survives being pasted into a bigger
  chapter file without adding visual noise. Values are escaped so a `-->` in a
  source can't terminate the comment early.
- **The Chapter line is a literal placeholder.** The chapter number isn't
  derivable from the video; a plausible-looking wrong guess is worse than an
  obvious blank.
- **The 4-space indent inside `<details>` is deliberate**, reproducing what the
  existing chapter files do (most renderers show it as a code block). Don't
  "fix" it.
- Applies to `lecture-*` and `tutorial-*` prompts only (`document.wants_wrapper`);
  `meeting-*` keeps the plain executive format. Override with
  `--format always|never`.
- `--combine` concatenates several documents with one Chapter line at the top,
  in **input** order — runs finish out of order when several go at once.

## Cross-session queueing (`lib/slotqueue.py`)

`--jobs` throttles concurrency *within one* `pipeline.sh` invocation. It does
nothing about several invocations running at once, which is the normal case
here (multiple terminals, or the trigger server firing repeatedly). The slot
queue is the machine-wide coordination those separate processes share, via
files under `/opt/meeting-bot/queue/`.

- One FIFO queue per component: `record`, `fetch_video`, `transcribe`,
  `frames`, `summarize`. Limit is `QUEUE_SLOTS_<COMPONENT>`, falling back to
  `QUEUE_SLOTS_DEFAULT`.
- **Unlimited unless configured.** Unset means the acquire path returns
  immediately and touches no files at all — the queue is opt-in, and an
  unconfigured box behaves exactly as it did before the queue existed.
- **A slot is held by the calling shell's PID**, not by a supervising process.
  That's what lets a bash stage hold a slot for an hour without a babysitter.
  Holders whose PID is gone are pruned by the next caller, so a SIGKILL'd run
  or a reboot releases its slot with no cleanup daemon — the queue cannot wedge
  permanently.
- A waiter whose own holder PID dies aborts instead of taking a slot nobody
  will use or release.
- `run_stage` in `run_one.sh` reads `$BASHPID` **into a variable before** the
  `$( ... )` command substitution. Inside the substitution, `$BASHPID` is the
  substitution's own throwaway subshell, which exits immediately — the queue
  would see a dead holder and reclaim the slot instantly, serializing nothing.
  This was a real bug; don't inline it back.
- `QUEUE_SLOTS_RECORD` is supported but dangerous and documented as such:
  recording is the only time-sensitive stage, so a queued meeting isn't delayed,
  it's missed. It stays unlimited by default.

## Things future Claude MUST NOT change

Decisions with a specific reason behind them. If you want to change one, stop
and confirm with the user first — they're deliberate trade-offs, not laziness.

- **Playwright uses `channel="chrome"`**, NOT the bundled Chromium. The bundled
  build gets Google's "This browser or app may not be secure" block on sign-in.
- **Login uses a direct `google-chrome` launch, not Playwright.** Even with
  `channel="chrome"`, Playwright injects automation flags (DevTools Protocol,
  `navigator.webdriver=true`) that Google detects. Do not route the login
  through Playwright.
- **The browser stages run in the Debian container.** This is what makes real
  Chrome possible on a musl host. Do not "simplify" it by switching to Alpine's
  `chromium` package or a glibc shim without explicit sign-off — the first
  reintroduces the sign-in block, the second is unsupported by both Chrome and
  Playwright.
- **Chrome runs with `--no-sandbox`.** Required because everything runs as root
  in the container. Sandbox + root = crash on launch.
- **Locale is `th-TH`**, so Thai participant names render in chat. Side effect:
  Meet's UI labels come back in Thai, which is why `capture.py` carries both
  English and Thai labels for every selector.
- **The kill switch routes through the in-Meet Leave button**, not by killing
  the browser process, so other participants see the bot leave cleanly.
  `kill_meeting.sh` force-removes a container only as post-grace escalation.
- **H.264 is `libx264 -preset ultrafast -crf 28`.** Keeps CPU low on a 4-vCPU
  VM with no GPU; visually fine for talking heads and slides.
- **The frame-sampling combo is scene-change + periodic.** Scene-change catches
  slide transitions and shared-video cuts; periodic guarantees a frame every N
  seconds on a static slide. Don't drop the periodic pass.
- **`--disable-features=ScreenCapture` is intentional** — the bot has no reason
  to share its screen. Layers 2 and 3 in `capture.py` (dialog killer, "Stop
  presenting" monitor) are the catch-nets if Chrome renames the flag.
- **Camera/mic mute is best-effort (log warning + continue), not abort.** A
  failed UI heuristic must not block a real meeting.
- **Google Meet pre-join uses a Tab-scan, not fixed Tab counts.** The pre-join
  DOM reorders frequently; identifying buttons by accessible name is the only
  durable approach.
- **YouTube URLs auto-route to youtube-transcript.io**, not AssemblyAI. We
  already have free captions there and they return in seconds.
- **Empty YouTube transcripts fail loudly**, not silently. Every key hits the
  same upstream captions, so retrying won't help. Placeholder-only text (e.g.
  `[เสียงพากย์ไทย]`) is written through so the operator can see it in the
  `.txt`; only a genuinely empty response is an error.
- **Multiple youtube-transcript.io accounts rotate round-robin** via the key
  file. Don't collapse this to a single env var — multi-account quota spreading
  was an explicit request.
- **The YouTube download never merges streams.** The format chain is
  `best[ext=mp4]/best/bv*[ext=mp4][vcodec^=avc1][height<=720]/bv*[ext=mp4][height<=720]/bv*[height<=720]/bv*`
  — muxed first, then **video-only**. NOT `bestvideo+bestaudio
  --merge-output-format mp4`: the merge path needs a JS runtime (deno) for
  YouTube extraction and a clean postprocess merge. Dropping audio is free
  here because this file exists *only* to extract frames from — the YouTube
  transcript comes from captions and never touches it. `avc1` is preferred
  over AV1 so a 4-vCPU box decodes frames cheaply (AV1 works, just slower).
  History: format 18 (muxed 360p) resolved fine in 2026-08, but by 2026-09
  YouTube exposes no muxed format at all on many videos and `best[ext=mp4]/best`
  alone fails with "Requested format is not available". Keep any future fix
  runtime-agnostic rather than re-enabling the merge.
  The same string appears in `lib/run_one.sh` and `summarize/summarize.py` —
  change both.
- **`summarize.py` does not download the video when `--frames-manifest` is
  given.** The video exists only to produce frames; once a manifest exists
  there's nothing to download. The pipeline always passes one, so re-adding the
  download would make every YouTube run fetch the same video twice.
- **Artifact paths derive from the run id, not the clock.** Resume depends on
  it. This is why `--out-base` exists.
- **`runstate.py status` verifies artifacts exist on disk** before reporting
  `done`. Don't "optimize" this away.
- **A stale `run.lock` is taken over, not fatal.** Otherwise a killed run could
  never be resumed.
- **Missing credentials raise `BackendUnavailable`, not `SystemExit`.**
  `SystemExit` doesn't inherit from `Exception`, so the fallback chain didn't
  catch it and one unset key killed the whole run. Keep it a normal exception.
- **Retry jitter is full, not proportional.** Parallel chunk requests must not
  retry in lockstep.
- **The document wrapper is built in code, not requested in the prompt.** See
  the output-format section above.
- **Queue slots default to unlimited.** Turning any of them on by default
  would silently serialize existing setups, and for `record` would silently
  start missing overlapping meetings. Opt-in only.
- **The queue holder is the calling shell's PID, read before the command
  substitution.** See the queueing section above — inlining `$BASHPID` into
  `$( ... )` breaks serialization in a way that looks like it works.
- **youtube-transcript.io tokens stay in the JSON keys file, not `.env`.**
  Multiple accounts are an array and the rotation cursor lives beside the keys
  it belongs to. The error message when it's empty explains this at length
  because operators reasonably look in `.env` first.
- **`whisper.cpp` is gone.** It hadn't been on any pipeline path since the
  AssemblyAI switch, and `setup.sh` no longer builds it. Don't re-add a
  `TRANSCRIBE_BACKEND=whisper` escape hatch without explicit sign-off.
- **Ubuntu/Debian host support was dropped.** `setup.sh` is apk-only and fails
  fast on a non-Alpine host with a pointer to git history. Don't reintroduce
  dual-target detection without asking.
- **Alpine has no systemd.** `meeting-bot-trigger.service` is kept for systemd
  hosts; an Alpine deployment needs an OpenRC init script.
- **`setup.sh` runs under bash, not ash.** Its one bash-only construct is the
  `local -a` arrays in `docker/recorder_lib.sh` (which Alpine's `ash` rejects
  with `syntax error: unexpected "(" (expecting "}")` at parse time, *before*
  the `docker build` line ever runs). Bash is already an apk dep — the cost of
  not re-shebanging is a silent break at the recorder-build step on every fresh
  install. Don't drop it back to `#!/bin/sh`.
- **deno is optional and installed from apk, not GitHub.** Deno's official
  release tarballs are glibc-linked and will not run on musl.

## Tests

All run without API keys, network, or `/opt`, against temp directories.

| File | Covers | Count |
|---|---|---|
| `lib/test_runstate.py` | state transitions, stale artifacts, concurrent writes, CLI | 13 |
| `lib/test_slotqueue.py` | FIFO order, dead-holder reclaim, timeout, CLI | 23 |
| `summarize/test_summarize_units.py` | retry classification/backoff, chunking, map-reduce, document | 31 |
| `lib/test_pipeline_e2e.sh` | full orchestration with stubbed stages, incl. two concurrent sessions | 77 |
| `transcribe/test_yt_transcript_client.py` | key rotation and retry | — |

`test_pipeline_e2e.sh` is the important one: it runs the real `pipeline.sh` and
`run_one.sh` and stubs only the four expensive stages, behind the same
argument/output contract. It has already caught four real bugs (`--from-file`
with no positionals, an unhelpful unrecognized-input error, the double YouTube
download, and the `$BASHPID`-in-substitution queue bug). Add to it when you touch orchestration.

**What no test here covers:** Chrome actually joining a live Meet/Zoom call,
real AssemblyAI/Gemini/youtube-transcript.io round-trips, ffmpeg's x11grab and
PulseAudio inside the container on real hardware, and the container image build
itself. Those need the real box and real keys.

## File layout

```
.
├── README.md                     <- all user-facing docs (the only other .md)
├── CLAUDE.md                     <- this file
├── .env.example
├── source_env.sh
├── setup.sh                      <- Alpine/apk, builds the recorder image
├── audio-setup.sh                <- runs INSIDE the container
├── first_time_login.sh           <- noVNC login via the container
├── kill_meeting.sh               <- per-run or global, container-aware
├── pipeline.sh                   <- multi-input orchestrator
├── trigger_server.py
├── meeting-bot-trigger.service   <- systemd hosts only; Alpine needs OpenRC
├── docker/
│   ├── Dockerfile.recorder       <- Debian + real Chrome
│   ├── recorder_lib.sh           <- shared container launcher
│   └── login_entry.sh            <- runs INSIDE the container
├── lib/
│   ├── runstate.py               <- run state + locking + CLI
│   ├── slotqueue.py              <- machine-wide component queue
│   ├── run_one.sh                <- the per-run stage DAG
│   ├── test_runstate.py
│   ├── test_slotqueue.py
│   └── test_pipeline_e2e.sh
├── screen/
│   ├── record_screen.sh          <- host wrapper
│   ├── record_in_container.sh    <- runs INSIDE the container
│   ├── capture.py                <- Playwright join driver
│   └── extract_frames.py
├── transcribe/
│   ├── transcribe.sh
│   ├── assemblyai_client.py
│   └── yt_transcript_client.py
└── summarize/
    ├── summarize.py
    ├── llm_client.py
    ├── retry.py
    ├── chunking.py
    ├── mapreduce.py
    ├── document.py
    ├── test_summarize_units.py
    └── prompts/
        ├── summarize.md          <- default
        ├── lecture-{claude,gemini}.md
        ├── tutorial-{claude,gemini}.md
        ├── meeting-{claude,gemini}.md
        └── _merge.md             <- internal; leading _ keeps it off the menu
```

## Things future Claude might want to add

- Auto-upload summaries to Slack / Notion / Obsidian after `pipeline.sh`.
- An OpenRC init script for `trigger_server.py` (Alpine has no systemd).
- Real-time incremental summary during a meeting (needs a long-running agent;
  the pipeline is post-meeting only).
- Speaker diarization, so summaries can attribute quotes without inferring.
- A web UI for re-summarizing a past run with different settings.
