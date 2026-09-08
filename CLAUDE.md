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

### Note on Claude: this project does NOT use the Anthropic API
The summarizer spends the operator's **Claude subscription** by running the
`claude` CLI as a subprocess. There is no `ANTHROPIC_API_KEY`, no `anthropic`
SDK in `requirements.in`, and no Messages-API call anywhere in the tree — so
`output_config`, `thinking`, `budget_tokens` and the rest of that surface are
not this project's problem. If you are about to reach for the SDK, read the
summarize section below first: removing it was deliberate and recent.

If you ever *do* add an API-key path back, load the `claude-api` skill first —
that request surface moved twice in 2025-2026 and memorized patterns are wrong.

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
pre-recorded API (or youtube-transcript.io for YouTube URLs), and produces a
Claude summary (through the `claude` CLI, on a subscription — no API key) — Markdown plus a PDF with the cited keyframes cropped to the slide and
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

### Which directories are disposable, and where they should live

Settled 2026-09-07 by inspection of this box. Two separate questions get
confused here, so keep them apart.

**The directories themselves are recreated on every run.** `lib/run_one.sh`
calls `paths_mkdir` on all five before the DAG starts, and `summarize.py`
resolves `SUMMARIES_DIR`/`PDF_DIR`/`FRAMES_DIR` with `create=True`. Deleting an
empty output directory is a no-op; the next run makes it again. `paths_require`
only checks that the *variable* is set, never that the path is sane.

**Their contents are not equally disposable.** This is the table that matters
when deciding what to put on which disk:

| Directory | Regenerating it costs | Safe to wipe? |
|---|---|---|
| `RECORDINGS_DIR` | **impossible** — the meeting is over | No. This is the irreplaceable one |
| `TRANSCRIPTS_DIR` | an AssemblyAI charge, per file | Only if you'll pay again |
| `FRAMES_DIR` | CPU only — ffmpeg re-reads the MP4 | **Yes** |
| `SUMMARIES_DIR` | a summarize run (subscription quota) | Prefer not |
| `PDF_DIR` | free, from the `.md` + the frames | Yes, *if* the frames still exist |

Frames are the one genuinely disposable set, because the source video always
outlives them: a recording sits in `RECORDINGS_DIR`, and a YouTube download
sits in `runs/<run_id>/video.mp4`. Losing frames costs one ffmpeg pass, and
`runstate.py status` already re-runs the stage when the artifacts are gone.

**Don't put `FRAMES_DIR` on `/tmp` here** — though the reason is durability,
not size. `/tmp` on this host is **tmpfs**: 3.9GB of RAM, no disk behind it.
Measured against real runs on this box (`$FRAMES_DIR/*/manifest.json`), frames
are 27-113KB each and `FRAME_PERIOD_SECONDS=30` yields ~120/hour, so a 3-hour
lecture is only 10-40MB. RAM pressure is therefore **not** the problem for one
run; it only becomes one if runs accumulate and nothing sweeps them. The real
costs are:

- tmpfs is empty after a reboot. Re-rendering a PDF from a summary you already
  have (`summarize/pdf.py summary.md out.pdf --frames-manifest ...`) then has
  no manifest and no images, and silently produces a PDF with no pictures in
  it — the run itself already succeeded, so nothing flags it.
- A pipeline resume after a reboot re-extracts frames it had already paid for
  (~14 min of CPU for a 3-hour video, measured; see the capacity notes below).

Put it on the local disk instead — `/opt/meeting-bot/frames` is the default and
is correct. Keeping frames *off* a network mount is right; tmpfs is the wrong
way to do it. If RAM-backed frames are ever wanted deliberately, size the tmpfs
and say so in `.env`, don't inherit the host's `/tmp`.

### Measured capacity of this box (4 vCPU QEMU, 7.8GB RAM, 15GB disk)

Benchmarked 2026-09-07 at the recorder's real settings (1920x1080, 15fps,
`libx264 -preset ultrafast -crf 28`, `aac 128k`), so a future session can size
a job without re-measuring:

| | Measured | A 3-hour lecture |
|---|---|---|
| Encoding | 120s of video in 53s wall = **2.3x realtime** | fits live, but two concurrent recordings leave almost no margin |
| Frame extraction | 120s of video in 9s = **13x realtime** | ~14 min of CPU, and it is the CPU-bound stage |
| Recorded bitrate | 342 kbps on static slides | ~0.5GB of slides; 1-3GB with a live camera |
| Frame size | 27-113KB per 1920x1080 JPEG | 360 frames = 10-40MB |
| Transcript | median **40,000 Thai chars/hour** across 31 past runs | ~120,000 chars = ~5 chunks + 1 merge |

The frame count is what drives summarize cost, because
`CLAUDE_CLI_FRAME_VISION=1` lets the model open each one: a 1920x1080 image is
about 1,844 tokens after Claude's downscale, so **360 frames is ~660k tokens if
the model reads all of them**. It does not have to — with the CLI it chooses —
but nothing caps it, and one chunk covering 36 minutes carries ~72 frames
(~133k tokens) which alone crowds a 200k context. For long lectures raise
`FRAME_PERIOD_SECONDS` (60-90 is plenty for slides) rather than relying on the
model to be sparing. `SCENE_THRESHOLD=0.3` contributes very little here — real
runs show 0-5 scene-change frames per video, so the periodic pass is
effectively the whole budget.

**A network mount under an output directory needs the mount checked, not just
the path.** This box points four of the five at `/mnt/My Libraries/...`, a
SeaDrive FUSE mount. Writes there are locally cached and fast (measured
328 MB/s), so ffmpeg writing a recording straight to it is fine. The hazard is
different: if seadrive is *not* mounted when a run starts, `/mnt` is an
ordinary empty directory on the root filesystem, `paths_mkdir` happily creates
the tree inside it, and the run writes a real recording and a real summary to
local disk — which the mount then hides the moment it comes back. Nothing
errors. Nothing is reported. The operator looks in the library and the lecture
isn't there. If this bites, the fix is a liveness check (a marker file that
must already exist inside each configured directory) rather than a `mkdir`.

### API keys are numbered slots with a persisted cursor

`lib/keyring.py`. `GEMINI_API_KEY_1..3`, `ASSEMBLYAI_API_KEY_1..3` and
`YT_TRANSCRIPT_KEY_1..10`; the unnumbered name is accepted as slot 1 so older
`.env` files keep working.

**There is no Anthropic row in the ring, deliberately.** The primary summarizer
authenticates as a Claude subscription through the `claude` CLI's own OAuth
login, which lives in `~/.claude`, not in `.env`. `keyring.py status` used to
list `ANTHROPIC_API_KEY` and that invited operators to set one — a set
`ANTHROPIC_API_KEY` silently moves the spend to a metered console account.
`./verify_e2e.sh --preflight` checks `claude auth status` in its place.

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

### Python dependencies are pinned, and uv is optional

`requirements.in` / `requirements-browser.in` are the files a human edits;
`requirements.txt` / `requirements-browser.txt` are generated from them by
`uv pip compile --generate-hashes` and are what `setup.sh` installs. Never edit
the generated files by hand — regenerate them (the command is in
`requirements.in`'s header).

**The compile command carries `--python-version 3.13 --python-platform
x86_64-unknown-linux-gnu`.** Without them uv resolves for whatever interpreter
is on the machine doing the compiling, which is not necessarily the box that
installs the result — the pins in git are for the deployment target, not for
someone's desktop.

The heaviest entry is **matplotlib**, and it is there for exactly one thing:
`mathtext`, the LaTeX typesetter behind `summarize/mathrender.py`. It brings
numpy with it. If that ever needs justifying: WeasyPrint has no JS engine and
no MathML, so without it the PDF prints the model's LaTeX as source. It is
optional at runtime — a venv without it renders maths as text and still
produces a PDF.

Two files, not one, because `setup.sh --no-chrome` builds a box that never
opens a browser and shouldn't carry playwright's bundled Node driver. The
browser file is compiled with `-c requirements.txt` so shared transitive deps
(typing-extensions today) land on the same version in both.

**`setup.sh` uses uv when it is on PATH and pip when it isn't.** uv installs
the same pinned set about 40x faster (4s vs 2m43s cold, measured on the target
box), but it is not in Debian's archive — it comes from astral.sh — so it
cannot be a hard requirement of a script whose whole job is bootstrapping a
fresh machine. Both paths verify the hashes, so the resulting venv is identical.
The venv itself is still created by `python3 -m venv`, not `uv venv`: a uv-made
venv has no `pip` inside it, and the pip fallback plus the troubleshooting
steps in README both need one.

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
- `pdf.py` + `framecrop.py` + `mathrender.py` — the PDF export, its frame
  cropping and its LaTeX typesetting.

Backends: `claude-cli` (default, aliases `claude`/`anthropic`/`cli`/`fcc`) and
`gemini`. `SUMMARY_BACKEND=fallback` is the default mode and walks
`SUMMARY_FALLBACK_CHAIN` (default `claude-cli,gemini`). **NVIDIA NIM and Ollama
were removed** in the Debian 13 port — neither had been on a configured path,
and both carried env surface and untested code. **The API-key `anthropic`
backend was removed** when the summarizer moved to the subscription; the name
survives only as an alias of `claude-cli`, so an existing `.env` whose chain
reads `anthropic,gemini` keeps working.

### The summarizer spends a subscription, not an API key

`summarize_claude_cli` runs `claude -p` as a subprocess and reads the JSON
envelope back. A Claude Pro/Max subscription has no API key — `api.anthropic.com`
bills a separate console account — and the CLI is the supported way to spend a
subscription non-interactively. That is the whole reason for the subprocess.

The invocation is fixed in one place and asserted in two test suites:

```
claude -p --output-format json --model <CLAUDE_CLI_MODEL> --effort <SUMMARY_EFFORT>
       --safe-mode --no-session-persistence
       [--tools Read --allowedTools Read --add-dir <frames dir>]
```

- **The prompt goes in on stdin, never in argv.** An 80KB transcript in an
  argument is over `ARG_MAX` on any normal box.
- **`--effort` is where `SUMMARY_EFFORT` lands.** Same scale the Messages API
  spells `output_config.effort`. There is no token-budget knob and no CLI
  spelling for one, which is a more durable fix than remembering not to send
  `budget_tokens`.
- **`--model` defaults to the alias `opus`, not a pinned id.** The CLI resolves
  aliases to the current model, so a rename doesn't 404 a box nobody has
  touched in a year. `ANTHROPIC_MODEL` is still read as a fallback name.
- **`--safe-mode` plus a scratch cwd** (`$MEETING_BOT_ROOT/tmp/claude-cli-cwd`).
  Either alone would do it; both are cheap. The CLI auto-discovers `CLAUDE.md`
  from its working directory, and *this* file is 38KB of architecture notes
  that have nothing to do with summarizing a lecture — it would be pulled into
  the context of every single summary. Controlling the cwd doesn't depend on a
  flag name staying put; `--safe-mode` also drops hooks, plugins and MCP
  servers.
- **`--no-session-persistence`**, or every summary leaves a full transcript in
  `~/.claude/projects` on a 15GB disk.

**The subprocess environment is scrubbed** (`_claude_cli_env`): `ANTHROPIC_API_KEY`,
`ANTHROPIC_API_KEY_1`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are
removed before launch. This is the most important line in the file. If any of
them survives, the CLI switches from subscription auth to API-key billing and
*says nothing* — the summary is identical and the charge lands on an account
the operator thought was unused. An empty `ANTHROPIC_API_KEY` is worse: it fails
auth in a way that reads like a broken subscription. `lib/fake_claude_cli.py`
records what reached the child process precisely so this regression is visible.

**Frames are read from disk, not inlined.** The Messages API took base64 image
blocks; the CLI takes a string. So the manifest carries absolute paths, the CLI
gets `--tools Read --allowedTools Read` scoped by `--add-dir` to the run's frame
directory, and the model opens the images itself. `--allowedTools` is required
because `-p` mode cannot answer a permission prompt. `CLAUDE_CLI_FRAME_VISION=0`
sends the manifest as text only — cheaper against a subscription's rate limit,
but then the model cites frames it has never seen and the pictures in the PDF
may not match the text.

**The CLI exits 0 when it is not logged in.** The only signal is the body:
`is_error: true` with `result: "Not logged in · Please run /login"`. So
`_run_claude_cli` parses the JSON rather than trusting the status, and maps
auth wording to `BackendUnavailable` so the chain advances to Gemini
immediately. Everything else becomes `ClaudeCliError` carrying the CLI's own
words, because the CLI has no HTTP status and `retry.py` classifies on wording.

**Gemini key rotation happens outside `with_retries`.** retry.py handles "the
provider is busy"; the rotation loop handles "this key is exhausted or
revoked". Inside the retry wrapper, a dead key would burn the full backoff
schedule before the chain ever advanced.

**`BackendUnavailable` carries `retryable = False`, and `retry.is_retryable`
honours that before every other check.** It is raised from *inside*
`with_retries` (the CLI only reveals "not logged in" once it has run), and
`is_retryable`'s type-name heuristic reads the "Unavailable" in the class name
as a busy server. Without the opt-out, a signed-out CLI sat through the full
backoff schedule before the chain ever reached Gemini. Caught by
`test_not_logged_in_is_never_retried`.

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

Reworked 2026-09-08 after the operator read a real 39-page output. Four
decisions came out of it, and each is load-bearing:

- **Keyframes are an appendix, not illustrations.** `PDF_FRAMES=contact` (the
  default) leaves every citation as the model wrote it and puts the frames it
  names into a thumbnail contact sheet in Appendix A. Inline figures were the
  export's worst feature: a keyframe is a screenshot of a video call, so most
  of them are a face, a half-drawn slide, or solid black — and the
  scene-change pass is *drawn to* the black ones, because black-to-content is
  the largest scene change in the video. `inline` restores the old behaviour,
  `none` drops frames entirely.
- **Only cited frames are cropped, and blank ones are dropped.**
  `_cited_frame_numbers` scans the rendered HTML with a looser regex than
  `FRAME_CITE_RE` so the second and third number of a compound citation
  ("Frame 33 @ ..., Frame 15 @ ...") count too, and `_prepare_frames` takes a
  `wanted` set. A three-hour manifest is hundreds of frames and cropping is
  the expensive part of this file; this made a real render 34s instead of
  minutes. `framecrop.is_blank` is the black-frame filter.
- **The transcript is an invisible layer, not an appendix.** White, 1pt,
  between `BEGIN_TRANSCRIPT` and `END_TRANSCRIPT` markers, in normal flow —
  *not* `display: none`, which would put nothing in the PDF at all. The reader
  never sees it; `pdftotext` always finds it.
  **It is cut into 40,000-character pieces on purpose.** Poppler silently
  stops returning text after roughly 50,000 characters on a single page:
  measured here, one 85k-character block came back 60% complete from
  `pdftotext` while pypdf read all of it off the same page. Since this repo's
  own `resources.py` shells out to `pdftotext`, a silent 40% loss was not an
  option. The cost is a couple of blank-looking pages at the back.
  `PDF_TRANSCRIPT=appendix` prints it as Appendix C; `none` omits it.
- **Body text is Adwaita Sans at 8pt, and every other size is an `em`.**
  `PDF_FONT_SIZE` therefore rescales headings, tables, captions and code
  together instead of leaving them stranded at their old point sizes. Arial
  and Liberation Sans sit behind Adwaita in the stack (Arial for a box that
  has it; Liberation is what "Arial" resolves to on Debian), and **Noto Sans
  Thai must stay in any custom stack** — Adwaita has no Thai glyphs, and a
  Thai lecture then renders as tofu.

The older decisions still hold:

- **In `inline` mode, the first citation of each frame becomes the image** and
  later ones stay text — a lecture that refers back to one diagram eight times
  should not print it eight times.
- **Figures are hoisted out of the block they were cited in** (`_end_of_block`)
  so a `<figure>` never lands inside a `<p>`, `<td>` or `<li>`, which produces
  invalid nesting and wrecks table layout.
- **A PDF failure is a warning, not a failed stage.** The markdown is already
  written by then and is what everything downstream depends on. Turning off
  *both* outputs is an error rather than a run that writes nothing.
- `run_one.sh` records whichever of the two files actually exists as the
  stage's artifacts — see the `--no-pdf` / `--no-markdown` paths.
- **`render()` owns the crop scratch directory unless the caller names one**,
  and everything after the `mkdir` runs inside the `try` whose `finally`
  deletes it. `summarize.py` must *not* pass `work_dir`: it used to, which
  made every run leave a `.frames` tree of cropped intermediates beside the
  deliverable — re-uploaded on every run of a synced `PDF_DIR`, and read by
  nothing, because WeasyPrint copies the image bytes into the PDF itself.
  `test_media_e2e.sh` asserts both halves of this.

### LaTeX in the PDF (`summarize/mathrender.py`)

The model writes maths; markdown renderers show it and WeasyPrint printed the
backslashes, because it has no JavaScript engine (so no KaTeX/MathJax) and no
MathML support. matplotlib's `mathtext` closes that gap: a self-contained
LaTeX-subset typesetter that ships **Computer Modern** (`fontset = "cm"`),
needs no TeX installation, and renders to SVG which WeasyPrint embeds happily.
That is the whole reason matplotlib is in `requirements.in`.

Non-obvious parts, all of them regression-tested:

- **Extraction runs on the markdown, before the HTML conversion.** Convert
  first and python-markdown has already eaten `_{trans}` into emphasis and
  dropped the backslashes. The maths comes out into opaque alphanumeric tokens
  (`MTHX3Z`) that markdown has no reason to touch, and goes back in *after*
  the citation passes so those never step over a base64 data: URI.
- **Baseline alignment is computed, not guessed.** `MathTextParser` reports
  width, height and depth; depth becomes a negative `vertical-align` in
  points, so inline maths sits on the text baseline instead of floating.
- **`\begin{aligned}` is split here.** mathtext has no environments at all —
  `\begin` is an unknown symbol to it — so multi-row display maths is broken
  on `\\` and rendered a row at a time, stacked.
- **Digits are wrapped in `\mathrm{}` outside `\text{}` groups.** With
  `mathtext.default = "it"` matplotlib italicises digits, which LaTeX does
  not, so `2 \times 10^8` came out visibly wrong. The rewrite is cosmetic, so
  a failed parse retries with the author's own spelling before falling back.
- **Nothing here may fail the render.** matplotlib is optional and its parser
  rejects real LaTeX (`\begin{cases}`, `\substack`); every failure degrades
  to cleaned-up text in a serif face. Same rule as the rest of the export.
- Identical expressions render once — a lecture writing `$L$` forty times pays
  for one SVG.

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

`is_blank()` lives here too, and is the same idea one step blunter: a frame
whose downscaled grayscale copy is within a few levels of one shade carries no
picture, so it never reaches the PDF. Recordings are full of solid-black
frames — a screen share stopping, a slide mid-fade — and the scene-change pass
collects them preferentially, since black-to-content is the biggest scene
change in the video.

Analysis runs on a 200px-wide grayscale copy, so cost is a few milliseconds per
frame regardless of source resolution. Pillow is optional: without it frames
are copied through uncropped with one warning, and nothing is blank.

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
- **The summarizer runs the `claude` CLI; it does not call the Messages API.**
  No `ANTHROPIC_API_KEY`, no `anthropic` package. Adding one back moves the
  spend off the subscription the operator is paying for. If a run has to be
  billed to a console account, that is a new backend beside `claude-cli`, not a
  change to it.
- **`ANTHROPIC_*` must stay scrubbed from the CLI's environment.** See the
  summarize section: leaving one set redirects billing silently, and an empty
  one breaks auth in a way that looks like a broken subscription.
- **The CLI runs with `--safe-mode` in a scratch cwd.** Otherwise this file
  gets loaded into the context of every summary.
- **The CLI's JSON body decides success, not its exit code.** It exits 0 when
  signed out. Parsing the envelope is the only way to tell.
- **`BackendUnavailable.retryable = False` stays.** Without it a signed-out CLI
  burns the whole retry schedule before falling back to Gemini.
- **No `thinking.budget_tokens`, ever** — and now no CLI spelling for one
  either. Effort is `--effort`; thinking is adaptive.
- **The document wrapper is built in code, not requested in the prompt.** See
  the output-format section above.
- **A failed PDF render must not fail the run.** The markdown is the artifact.
- **Unparseable LaTeX degrades to text; it never fails the render.** matplotlib
  is optional and its parser rejects plenty of real LaTeX.
- **Maths is extracted before the markdown→HTML conversion, not after.**
  Convert first and there is no LaTeX left to typeset.
- **The hidden transcript is chunked under poppler's per-page extraction
  limit**, and is white text in normal flow rather than `display: none`.
  Either mistake — one big block, or a display rule that emits nothing —
  turns "the transcript travels with the PDF" into a silent half-truth.
- **Keep a Thai face in `PDF_FONT_FAMILY`.** Adwaita Sans, Arial and
  Liberation Sans all lack Thai glyphs.
- **`summarize.py` must not pass `work_dir` to `pdf.render()`.** Naming one
  transfers ownership and leaves the cropped intermediates beside the
  deliverable.
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
| `summarize/test_summarize_units.py` | retry classification/backoff, chunking, map-reduce, document, the claude-cli command line + envelope parsing | 64 |
| `summarize/test_pdf_units.py` | crop geometry, citation rewriting, blank-frame detection, LaTeX extraction/fallback, the hidden transcript, real PDF render | 53 |
| `transcribe/test_yt_transcript_client.py` | key rotation, retry, and the `tracks[]` response shape | 16 |
| `lib/test_pipeline_e2e.sh` | full orchestration with stubbed stages, output dirs, PDF/markdown toggles, `--resources` | 106 |
| `lib/test_media_e2e.sh` | real MP4 + real SDKs against local stub servers, and the real llm_client against a stub `claude` binary | 62 |
| `verify_e2e.sh --browser-smoke` | real Chrome under Xvfb, recorded and measured for black edges | 6 |

`test_pipeline_e2e.sh` runs the real `pipeline.sh` and `run_one.sh` and stubs
only the four expensive stages, behind the same argument/output contract. It has
caught five real bugs so far (`--from-file` with no positionals, an unhelpful
unrecognized-input error, the double YouTube download, the
`$BASHPID`-in-substitution queue bug, and a stage exiting 0 without writing its
artifacts). Add to it when you touch orchestration.

`test_media_e2e.sh` is the counterpart: real media, real SDKs, stub servers
(`lib/fake_api_server.py`) speaking the providers' HTTP protocols. It is the
only place that can assert on **what we actually send**. For the summarizer
that seam is no longer HTTP — it is `lib/fake_claude_cli.py`, a stub
*executable* that `CLAUDE_CLI_BIN` points at. It records the argv, the piped
prompt and the auth vars that reached the child, so the test asserts that
`--effort` carries `SUMMARY_EFFORT`, that `--add-dir` scopes Read to the run's
frame directory, that absolute frame paths are in the prompt, and that
`ANTHROPIC_API_KEY` — deliberately exported by the test — did *not* reach the
CLI. It also drives `FAKE_CLAUDE_MODE=not-logged-in` to prove a signed-out CLI
fails as `BackendUnavailable` rather than being retried. When you change the
invocation, assert it here.

**What no test here covers:** Chrome actually joining a live Meet/Zoom call, and
real AssemblyAI/Claude/Gemini/youtube-transcript.io round-trips — including
whether the subscription behind `claude auth` has quota left.
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
├── requirements.in               <- edit this
├── requirements.txt              <- generated, hash-pinned; setup.sh installs it
├── requirements-browser.in       <- playwright only, for the browser stages
├── requirements-browser.txt      <- generated
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
│   ├── fake_api_server.py        <- stub AssemblyAI/YouTube servers
│   ├── fake_claude_cli.py        <- stub `claude` binary for the media test
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
    ├── pdf.py                    <- markdown -> PDF
    ├── framecrop.py              <- slide-region detection, blank frames
    ├── mathrender.py             <- LaTeX -> Computer Modern SVG
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
