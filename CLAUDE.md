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

### Note on the Anthropic API
Before writing Anthropic code, load the `claude-api` skill. The request surface
moved twice in 2025-2026 and memorized patterns are wrong: `budget_tokens` is
**rejected** by current models, effort is `output_config.effort`, and thinking
is `{"type": "adaptive"}`. See the summarize section below.

## Branches

- **`debian13`** (this one) — Debian 13 host, everything native.
- **`alpinelinux`** — the previous architecture, preserved verbatim: Alpine
  host plus a Debian container for the browser stages. Consult it before
  reintroducing anything container-shaped; it is also where the Docker files,
  `docker/recorder_lib.sh` and `audio-setup.sh` still live.
- `main` — the Alpine tree as it was when the port started.

## What this project is, in one paragraph

A meeting/lecture bot for a **Debian 13** guest on Proxmox. It joins a Google
Meet or Zoom call in a persistent real-Chrome profile (so Google's sign-in flow
doesn't get blocked by automation-detection heuristics), records both the screen
and the meeting audio into an MP4, transcribes the audio with the AssemblyAI
pre-recorded API (or youtube-transcript.io for YouTube URLs), and produces an AI
summary — Markdown plus a PDF with the cited keyframes cropped to the slide and
inlined — combining the transcript with keyframes extracted from the recording
and, optionally, the lecturer's own slides from a GitHub repo or a folder. It
accepts several inputs per invocation, runs them concurrently, and resumes
anything that failed partway.

## Everything runs on one host — read this first

The Alpine branch split the system in two because **Alpine is musl and Chrome is
glibc-only**: Google ships no musl build, and Playwright doesn't support Alpine
for its bundled browsers. Debian 13 is glibc, so `google-chrome-stable` installs
and runs natively and **the container split is gone**. No Docker, no image
build, no bind mounts, no `docker/` directory.

What the container used to provide for free was per-run isolation: every
recording had its own PID/IPC/network namespace, so display `:99` and a sink
called `meeting_sink` could be hardcoded and never collide. Natively there is
one X server and one PulseAudio daemon for the whole box, so both are now
allocated per run in `lib/xsession.sh`:

- **Display.** `xsession_pick_display` claims the first free number in
  `DISPLAY_MIN..DISPLAY_MAX` (90-119) by creating `/tmp/.X<n>-lock` with
  `set -o noclobber` — an atomic `O_EXCL` create. That is what makes two runs
  starting in the same second pick different numbers; a "check then start"
  scheme races. Xvfb is then started with `-nolock`, because the lock we just
  made would otherwise look to it like a server already running.
- **Audio.** Each run loads its own `module-null-sink` named after the run id,
  and Chrome is pointed at it with the **`PULSE_SINK` environment variable**.
  `pactl set-default-sink` is deliberately NOT used: the default sink is global
  state, and flipping it would move a concurrently-recording meeting's audio
  into this run's MP4. ffmpeg records `<sink>.monitor`.

If you are tempted to hardcode a display number again, don't — that only worked
because of the container boundary that no longer exists.

## Configuration

Non-secret env vars *and* the API keys live in a single `.env` at the repo root;
`.env.example` is the committed template. `.env` is gitignored.

**`.env.example` carries no explanatory comments on purpose** — just names and
defaults. Every explanation lives in README.md's Configuration section, so
there is one place to update when a default changes. Don't re-add prose to the
template.

`pipeline.sh`, `lib/run_one.sh`, `transcribe.sh`, `first_time_login.sh`,
`verify_e2e.sh` and `record_screen.sh` all source `source_env.sh`;
`summarize.py` carries its own `_load_dotenv()` for direct invocation. The
loader fills in unset values only — an already-exported var always wins.

### Output directories are five independent, required variables

`RECORDINGS_DIR`, `TRANSCRIPTS_DIR`, `FRAMES_DIR`, `SUMMARIES_DIR`, `PDF_DIR`.
None of them is derived from another or from `MEETING_BOT_ROOT`, which now holds
only the pipeline's own bookkeeping (`runs/`, `state/`, `tmp/`, `resources/`,
`chrome-profile/`). `lib/paths.py` and `lib/paths.sh` resolve them and **fail
with the variable's name if one is unset** rather than falling back to a
default. That is deliberate: with independent paths a wrong default doesn't
error, it silently writes the deliverable somewhere the operator will never
look. Any of them may contain spaces — the test suites use a root with a space
in it precisely so quoting regressions fail loudly.

### API keys are numbered slots with a persisted cursor

`lib/keyring.py`. `GEMINI_API_KEY_1..3`, `ASSEMBLYAI_API_KEY_1..3`,
`YT_TRANSCRIPT_KEY_1..10`, and a single `ANTHROPIC_API_KEY`; the unnumbered name
is accepted as slot 1 so older `.env` files keep working.

**The rotation cursor is on disk** (`$MEETING_BOT_ROOT/state/keycursor.json`,
written under an flock), not per-process. Rotation only spreads quota if
consecutive *processes* start on different keys — a per-process cursor sends
every run at key #1 and exhausts that account first. Every read and write of the
cursor is best-effort: a lost cursor costs one duplicated request, never a
failed run.

The youtube-transcript.io tokens used to live in
`/opt/meeting-bot/secrets/youtube_transcript_keys.json`. **That file is gone**
and its support is removed; `.env` is the only source. The error message says so
explicitly, because operators following an older README will go looking for it.

## The run model

Everything mutable about a run lives in `$MEETING_BOT_ROOT/runs/<run_id>/`:

```
runs/<run_id>/
  state.json      per-stage status + artifact paths   (lib/runstate.py)
  state.lock      flock target for read-modify-write
  run.lock/       mkdir-based single-writer lock + pid
  logs/<stage>.log
  kill            per-run kill sentinel
  admitted        per-run admission marker
  record.pid      record/join/ffmpeg pids + display + sink for this recording
  video.mp4       YouTube download, when applicable
```

`run_id` is `<safe_name>_<YYYYmmdd_HHMMSS>`, and **all artifact paths derive
from it** — never from a fresh timestamp. A resumed run must land on the same
filenames the first attempt used or it can't tell what already succeeded. This
is why `transcribe.sh` has `--out-base` and why `summarize.py` takes
`--pdf-out` (PDF_DIR is not derivable from the .md path — the two directories
are configured separately).

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
- The run's `resources` list is stored in `state.json` and replayed on every
  attempt, so a resume summarizes against the same slides.

## Per-stage reference

### Stage 1 — Recording (`screen/record_screen.sh`)

One script now, not a host wrapper plus an in-container body.

- Allocates a display and a sink (`lib/xsession.sh`), starts Xvfb, exports
  `DISPLAY` and `PULSE_SINK`, runs `capture.py`, waits for the `admitted`
  marker, then starts ffmpeg.
- **Geometry must agree everywhere**: the Xvfb head, Chrome's `--kiosk` window
  (`capture.py` reads `RECORD_GEOMETRY`), and ffmpeg's `-video_size`. A
  mismatch produces black edges. `--kiosk` alone isn't enough on some
  Xvfb/Chrome combos, which is why `--window-size` is also passed.
- Encoder: `libx264 -preset ultrafast -crf 28`, audio `aac -b:a 128k`.
- Writes `runs/<id>/record.pid` (record/join/ffmpeg pids, display, sink) so
  `kill_meeting.sh` can escalate against the right processes without guessing.
- Kill: the host touches `runs/<id>/kill`, `capture.py` sees it on the next
  poll and clicks Leave. `kill_meeting.sh` signals the recorded pids only after
  a grace period, and sends ffmpeg `SIGINT` (not `SIGKILL`) so the MP4 is
  finalised and playable.
- Failure artifacts (`join_failed.png`, `not_admitted.png`) go in the run dir,
  not a shared directory where the next run would overwrite them.

### Stage 2 — Transcribe (`transcribe/transcribe.sh`)

- Local files → AssemblyAI (`assemblyai_client.py`). MP3/MP4/M4A/WAV go
  directly; WEBM/OGG are demuxed to MP3 with ffmpeg first.
- **AssemblyAI segments are sentences, not words.** `transcribe_file` calls
  `transcript.get_sentences()` and falls back to `transcript.words` only if
  that fails. Word-level segments made the `.txt` one word per line — which
  is what gets embedded verbatim in the summary document — and the `.srt` one
  word per cue. Sentence granularity also matches what the YouTube backend
  emits, so the shared writer produces comparable output for both.
- **Key rotation is failure-class aware.** A key rejected for auth/quota
  reasons (`_is_key_level_error`) hands over to the next key; anything else —
  a silent file, an unsupported language — is raised immediately, because
  another key would fail identically and three uploads of the same video is a
  real cost.
- YouTube URLs → youtube-transcript.io (`yt_transcript_client.py`). No audio
  download; captions come back as `{text, offset_ms, duration_ms}` segments.
- **The timed segments live in `tracks[].transcript`**, as
  `{start, dur, text}` with seconds-as-strings. The entry's flat `text` field
  is the whole transcript in one string with no timing; parsing that instead
  (which is what the old fall-through did) yields a single segment, a useless
  `.srt`, and a chunker with no timestamps to assign frames by. `_pick_track`
  also honours a preferred language — but a track's `language` is a human
  label ("English - English"), so the ISO code has to come from the sibling
  `languages` array, which pairs label with `languageCode` in the same order.
  Matching the label directly against "en" always fails and falls back to
  `tracks[0]`. It only chooses among tracks the video already has; it never
  translates, and many videos expose just one track (often not English).
  Caption text arrives HTML-escaped (`&lt;i&gt;`, `&amp;`), so
  `_clean_caption_text` unescapes and drops the markup.
- Both feed one shared writer producing `.txt` + `.srt`.
- `--out-base PATH` overrides the timestamped default (see the run model).
- Language default `th`, override via arg or `ASSEMBLYAI_LANGUAGE`.
- `ASSEMBLYAI_BASE_URL` and `YT_TRANSCRIPT_API_URL` exist so
  `lib/test_media_e2e.sh` can run the real clients against local stub servers.
  They are test seams, not features — but they are also the only way to
  exercise these clients without spending money, so don't remove them.

### Stage 3 — Summarize (`summarize/`)

Split across seven modules:

- `summarize.py` — entry point and orchestration.
- `llm_client.py` — backend dispatch + the fallback chain.
- `retry.py` — transient-failure policy (503/429/5xx).
- `chunking.py` — splitting long transcripts, assigning frames to chunks.
- `mapreduce.py` — parallel chunk summarization + the merge call.
- `document.py` — the course-note document wrapper and `--combine`.
- `pdf.py` + `framecrop.py` — the PDF export and its frame cropping.

Backends: `anthropic` (default, aliases `claude`/`fcc`) and `gemini`.
`SUMMARY_BACKEND=fallback` is the default mode and walks
`SUMMARY_FALLBACK_CHAIN` (default `anthropic,gemini`). **NVIDIA NIM and Ollama
were removed** in the Debian 13 port — neither had been on a configured path,
and both carried env surface and untested code.

**Anthropic request shape.** `ANTHROPIC_MODEL` defaults to `claude-opus-5`.
`SUMMARY_EFFORT` maps onto `output_config.effort` (low|medium|high|xhigh|max),
and thinking is `{"type": "adaptive"}`. There is deliberately **no thinking-
budget setting**: `thinking.budget_tokens` is rejected with a 400 by every
current model. Both parameters are passed as normal keyword arguments and
retried inside `extra_body` if the installed SDK is too old to know them
(`_call_with_kwarg_fallback`) — a stale `pip install anthropic` on the box
should degrade, not fail the stage.

**Gemini key rotation happens outside `with_retries`.** retry.py handles "the
provider is busy"; the rotation loop handles "this key is exhausted or
revoked". Inside the retry wrapper, a dead key would burn the full backoff
schedule before the chain ever advanced.

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
default 30), deduplicated by ±half-period, into `$FRAMES_DIR/<run_id>/manifest.json`.
The pipeline runs this as its own stage and passes `--frames-manifest` to
`summarize.py`.

**Reference material** (`lib/resources.py`). `--resources` takes a GitHub URL
(optionally `@branch`, or a `/tree/<branch>/<subdir>` URL pasted from the
browser) or a local file/folder. Text from `.md/.txt/.pdf/.pptx/.docx` is
appended to the prompt template, capped at `RESOURCE_MAX_CHARS`; slide images
(pdftoppm, LibreOffice for pptx) go into the PDF's Appendix B.

Two non-obvious details:
- **Braces in the material are doubled before injection.** The prompt template
  is later run through `str.format()` for `{transcript}` and
  `{frame_manifest}`; an unescaped `{x}` in someone's slides raises KeyError
  and takes down a run that had already paid for transcription.
- **A missing *local* path is fatal; an unreachable GitHub repo is not.** A bad
  local path is always a typo, and finding out after paying for a summary is
  worse than failing in the first second. A repo that won't clone only degrades
  the summary, so it becomes a note.
- OOXML text is extracted with `zipfile` + a regex, not python-pptx/python-docx:
  we want the words, not the layout, and that is two fewer dependencies.

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

### The PDF (`summarize/pdf.py`)

WeasyPrint, markdown→HTML→PDF. Chosen over headless Chrome (which would couple
stage 3 to the browser half) and over pandoc/LaTeX (a gigabyte of texlive, and
Thai in LaTeX is genuinely painful).

- **The first citation of each frame becomes the image; later ones stay text.**
  The model cites frames as *(Frame 12 @ 410.0s)*; a lecture that refers back
  to one diagram eight times should not print it eight times.
- **Figures are hoisted out of the block they were cited in** (`_end_of_block`)
  so a `<figure>` never lands inside a `<p>`, `<td>` or `<li>`, which produces
  invalid nesting and wrecks table layout.
- **The transcript moves to Appendix A.** A PDF has no collapsed `<details>`,
  and 80KB of ASR output at the top buries the summary.
- **A PDF failure is a warning, not a failed stage.** The markdown is already
  written by then and is what everything downstream depends on. Turning off
  *both* outputs is an error rather than a run that writes nothing.
- `run_one.sh` records whichever of the two files actually exists as the
  stage's artifacts — see the `--no-pdf` / `--no-markdown` paths.

### Frame cropping (`summarize/framecrop.py`)

`slide` mode looks for the largest bright rectangle (slides are overwhelmingly
light on dark UI) and accepts it only if it passes **all** of: minimum side
(240px), minimum area (18% of the frame), aspect ratio in 0.9-3.2, brighter
than the frame as a whole, and not the whole frame. Otherwise it falls back to
a mechanical border trim, and then to no crop at all.

Every one of those guards is load-bearing, and two of them were written after
the tests caught real failures: without the size/area floor a white logo or a
cursor highlight becomes "the slide" and the PDF gets a 30-pixel thumbnail;
without the full-frame check, rounding in the downscaled analysis pass reports
a 2-pixel "crop" on every frame and re-encodes the lot for nothing. **A
confidently wrong crop is worse than an uncropped frame** — that is the whole
design rule here.

Analysis runs on a 200px-wide grayscale copy, so cost is a few milliseconds per
frame regardless of source resolution. Pillow is optional: without it frames
are copied through uncropped with one warning.

## Cross-session queueing (`lib/slotqueue.py`)

`--jobs` throttles concurrency *within one* `pipeline.sh` invocation. It does
nothing about several invocations running at once, which is the normal case
here (multiple terminals, or the trigger server firing repeatedly). The slot
queue is the machine-wide coordination those separate processes share, via
files under `$MEETING_BOT_ROOT/queue/`.

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
- **Login uses a direct `google-chrome-stable` launch, not Playwright.** Even
  with `channel="chrome"`, Playwright injects automation flags (DevTools
  Protocol, `navigator.webdriver=true`) that Google detects. Do not route the
  login through Playwright.
- **Chrome must be `google-chrome-stable`, not Debian's `chromium`.** The
  branded build is what gets through the sign-in flow. This is the requirement
  the whole Alpine-container era existed to satisfy; don't trade it away now
  that it's cheap to meet.
- **Chrome runs with `--no-sandbox`.** Required because everything runs as root.
  Sandbox + root = crash on launch.
- **`--window-position=0,0` stays in `CHROME_ARGS`.** Without it Chrome places
  its kiosk window at (10,10) and every recording carries a 10px black band
  down the left and top edges. Found by `verify_e2e.sh --browser-smoke`, which
  measures the recorded frame rather than trusting the reported window size —
  a 1px band at the right and bottom is Chrome's viewport rounding and is fine.
- **`CHROME_ARGS` in `capture.py` is the single source of the command line**,
  imported by `screen/browser_smoke.py`. A flag that breaks recording has to
  break the smoke test too, or the smoke test is testing a different browser.
- **Display numbers and sink names are allocated per run, never hardcoded.**
  The container boundary that made `:99` safe is gone. See the isolation
  section above, including why `pactl set-default-sink` must not be used.
- **Locale is `th-TH`**, so Thai participant names render in chat. Side effect:
  Meet's UI labels come back in Thai, which is why `capture.py` carries both
  English and Thai labels for every selector.
- **An unconfirmed join click is not a failed join.** `capture.py` falls
  through to `wait_for_admission()` whenever the click can't be confirmed but
  `join_rejection_reason()` finds no refusal on the page. Zoom's web client
  hides the button behind its "Joining Meeting..." interstitial, so
  `click_first_match` times out *while the join is succeeding* — the old
  fail-fast path abandoned calls the bot was seconds from entering. The
  refusal detector (English + Thai) is what keeps this from costing the full
  600s `ADMIT_TIMEOUT_SECONDS` on a genuinely dead link; it also runs inside
  the admission wait loop, so "no one responded to your request" fails fast.
- **The kill switch routes through the in-Meet Leave button**, not by killing
  the browser process, so other participants see the bot leave cleanly.
  `kill_meeting.sh` signals pids only as post-grace escalation, and gives
  ffmpeg `SIGINT` so the MP4 stays playable.
- **H.264 is `libx264 -preset ultrafast -crf 28`.** Keeps CPU low on a 4-vCPU
  VM with no GPU; visually fine for talking heads and slides.
- **The frame-sampling combo is scene-change + periodic.** Scene-change catches
  slide transitions and shared-video cuts; periodic guarantees a frame every N
  seconds on a static slide. Don't drop the periodic pass.
- **`--disable-features=ScreenCapture` is intentional** — the bot has no reason
  to share its screen. Layers 2 and 3 in `capture.py` (dialog killer, "Stop
  presenting" monitor) are the catch-nets if Chrome renames the flag.
- **EXIT traps in the `set -e` scripts end every line with `|| true`.**
  `record_screen.sh`, `first_time_login.sh` and `xsession_stop_xvfb` all kill
  pids that are usually already gone. Under errexit a failing command in an
  EXIT trap aborts the trap *and becomes the script's exit status* — which made
  every successful recording exit 1, so `pipeline.sh` marked `record` failed and
  never transcribed the MP4 it had just produced. Verified 2026-09.
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
- **Multi-key rotation is round-robin with an on-disk cursor.** Don't collapse
  any of the numbered key sets to a single variable, and don't make the cursor
  per-process — see the keyring section.
- **The YouTube download never merges streams.** The format chain is
  `best[ext=mp4]/best/bv*[ext=mp4][vcodec^=avc1][height<=720]/bv*[ext=mp4][height<=720]/bv*[height<=720]/bv*`
  — muxed first, then **video-only**. NOT `bestvideo+bestaudio
  --merge-output-format mp4`: the merge path needs a JS runtime for YouTube
  extraction and a clean postprocess merge. Dropping audio is free here because
  this file exists *only* to extract frames from — the YouTube transcript comes
  from captions and never touches it. `avc1` is preferred over AV1 so a 4-vCPU
  box decodes frames cheaply (AV1 works, just slower).
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
  it. This is why `--out-base` and `--pdf-out` exist.
- **The five output directories are required, with no defaults.** See the
  configuration section: a silent default is worse than an error here.
- **`runstate.py status` verifies artifacts exist on disk** before reporting
  `done`, and `mark_done` in `run_one.sh` refuses to record an absolute
  artifact path that doesn't exist yet. Don't "optimize" either away — they
  are what turns "the stage lied about succeeding" into an error naming the
  missing file, instead of a traceback two stages later.
- **A stale `run.lock` is taken over, not fatal.** Otherwise a killed run could
  never be resumed.
- **Missing credentials raise `BackendUnavailable`, not `SystemExit`.**
  `SystemExit` doesn't inherit from `Exception`, so the fallback chain didn't
  catch it and one unset key killed the whole run. Keep it a normal exception.
- **Retry jitter is full, not proportional.** Parallel chunk requests must not
  retry in lockstep.
- **No `thinking.budget_tokens`, ever.** Current Claude models reject it with a
  400. Effort is `output_config.effort`; thinking is adaptive.
- **The document wrapper is built in code, not requested in the prompt.** See
  the output-format section above.
- **A failed PDF render must not fail the run.** The markdown is the artifact.
- **Frame cropping declines rather than guesses.** See the framecrop section.
- **Reference material is escaped before it enters the prompt template**, and
  framed as data rather than instructions — it is untrusted input exactly like
  the transcript.
- **Queue slots default to unlimited.** Turning any of them on by default
  would silently serialize existing setups, and for `record` would silently
  start missing overlapping meetings. Opt-in only.
- **The queue holder is the calling shell's PID, read before the command
  substitution.** See the queueing section above — inlining `$BASHPID` into
  `$( ... )` breaks serialization in a way that looks like it works.
- **`whisper.cpp` is gone.** It hadn't been on any pipeline path since the
  AssemblyAI switch. Don't re-add a `TRANSCRIBE_BACKEND=whisper` escape hatch
  without explicit sign-off.
- **Alpine support was dropped in the Debian 13 port.** `setup.sh` is apt-only
  and fails fast elsewhere with a pointer to the `alpinelinux` branch. Don't
  reintroduce dual-target detection without asking.
- **Don't reintroduce Docker.** The container existed only to give Chrome a
  glibc filesystem. On Debian that is free, and the container cost a daemon, an
  image build, bind mounts, and a second copy of ffmpeg.

## Tests

All of these run without API keys, network, or `/opt`, against temp directories
— including one whose path contains a space, so quoting regressions fail loudly.
`verify_e2e.sh` is the exception: it is the live checklist.

| File | Covers | Count |
|---|---|---|
| `lib/test_runstate.py` | state transitions, stale artifacts, concurrent writes, CLI | 13 |
| `lib/test_slotqueue.py` | FIFO order, dead-holder reclaim, timeout, CLI | 23 |
| `lib/test_keyring.py` | numbered slots, gaps, duplicates, cursor persistence | 22 |
| `lib/test_resources.py` | spec parsing, text extraction, GitHub fetch, budgets | 27 |
| `summarize/test_summarize_units.py` | retry classification/backoff, chunking, map-reduce, document | 31 |
| `summarize/test_pdf_units.py` | crop geometry, citation rewriting, real PDF render | 23 |
| `transcribe/test_yt_transcript_client.py` | key rotation, retry, and the `tracks[]` response shape | 16 |
| `lib/test_pipeline_e2e.sh` | full orchestration with stubbed stages, output dirs, PDF/markdown toggles, `--resources` | 106 |
| `lib/test_media_e2e.sh` | real MP4 + real SDKs against local stub servers | 49 |
| `verify_e2e.sh --browser-smoke` | real Chrome under Xvfb, recorded and measured for black edges | 6 |

`test_pipeline_e2e.sh` runs the real `pipeline.sh` and `run_one.sh` and stubs
only the four expensive stages, behind the same argument/output contract. It has
caught five real bugs so far (`--from-file` with no positionals, an unhelpful
unrecognized-input error, the double YouTube download, the
`$BASHPID`-in-substitution queue bug, and a stage exiting 0 without writing its
artifacts). Add to it when you touch orchestration.

`test_media_e2e.sh` is the counterpart: real media, real SDKs, stub servers
(`lib/fake_api_server.py`) speaking the providers' HTTP protocols. It is the
only place that can assert on **what we actually send** — that
`output_config.effort` carries `SUMMARY_EFFORT`, that thinking is adaptive,
that `budget_tokens` is absent, that frames are attached as image blocks. When
you change the request shape, assert it here.

**What no test here covers:** Chrome actually joining a live Meet/Zoom call, and
real AssemblyAI/Anthropic/Gemini/youtube-transcript.io round-trips.
`./verify_e2e.sh` runs those on the real box. Its `--preflight` and
`--browser-smoke` need no keys and no meeting: between them they cover the
display/audio/capture chain and Chrome rendering under Xvfb with the recorder's
own flags, which is everything about stage 1 except the call itself.

## File layout

```
.
├── README.md                     <- all user-facing docs (the only other .md)
├── CLAUDE.md                     <- this file
├── .env.example                  <- names and defaults only; prose lives in README
├── source_env.sh
├── setup.sh                      <- Debian/apt, installs Chrome + the venv
├── first_time_login.sh           <- noVNC login, native Chrome
├── kill_meeting.sh               <- per-run or global, pid-file based
├── pipeline.sh                   <- multi-input orchestrator
├── verify_e2e.sh                 <- live checks: preflight + mp4/YouTube/Meet/Zoom
├── trigger_server.py
├── meeting-bot-trigger.service   <- systemd unit (setup.sh --with-trigger)
├── lib/
│   ├── runstate.py               <- run state + locking + CLI
│   ├── slotqueue.py              <- machine-wide component queue
│   ├── keyring.py                <- numbered API keys + rotation cursor
│   ├── paths.py / paths.sh       <- the five required output directories
│   ├── xsession.sh               <- per-run Xvfb display + PulseAudio sink
│   ├── resources.py              <- slides/notes from GitHub or a folder
│   ├── run_one.sh                <- the per-run stage DAG
│   ├── fake_api_server.py        <- stub Anthropic/AssemblyAI/YouTube servers
│   ├── test_runstate.py
│   ├── test_slotqueue.py
│   ├── test_keyring.py
│   ├── test_resources.py
│   ├── test_pipeline_e2e.sh
│   └── test_media_e2e.sh
├── screen/
│   ├── record_screen.sh          <- stage 1, native (no container)
│   ├── capture.py                <- Playwright join driver; owns CHROME_ARGS
│   ├── browser_smoke.py          <- the same browser, without a meeting
│   └── extract_frames.py
├── transcribe/
│   ├── transcribe.sh
│   ├── assemblyai_client.py
│   ├── yt_transcript_client.py
│   └── test_yt_transcript_client.py
└── summarize/
    ├── summarize.py
    ├── llm_client.py
    ├── retry.py
    ├── chunking.py
    ├── mapreduce.py
    ├── document.py
    ├── pdf.py                    <- markdown -> PDF, frames inlined
    ├── framecrop.py              <- slide-region detection
    ├── test_summarize_units.py
    ├── test_pdf_units.py
    └── prompts/
        ├── summarize.md          <- default
        ├── lecture-{claude,gemini}.md
        ├── tutorial-{claude,gemini}.md
        ├── meeting-{claude,gemini}.md
        └── _merge.md             <- internal; leading _ keeps it off the menu
```

## Things future Claude might want to add

- Auto-upload summaries to Slack / Notion / Obsidian after `pipeline.sh`.
- Real-time incremental summary during a meeting (needs a long-running agent;
  the pipeline is post-meeting only).
- Speaker diarization, so summaries can attribute quotes without inferring.
- A web UI for re-summarizing a past run with different settings.
- Slide-to-transcript alignment: match rendered slide images against extracted
  frames so the PDF can show the *source* slide rather than a screen capture of
  it.
