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
pre-recorded API (or youtube-transcript.io for YouTube URLs, or the entry's own
captions for a Kaltura embed), and produces a
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
                  (state.json's summarize stage also carries `usage`, and
                  `waiting_until` / `rate_limited` while paused on the
                  Claude usage window — see the summarize section)
  video.mp4       YouTube download, when applicable
  clip.mp4        the --clip window, cut from video.mp4 / the input
  kaltura.json    entry facts, cached at fetch time
  parts.json      combine run only: the members' transcripts + manifests, rebuilt per attempt
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

`clip` is a sixth stage, run only when `--clip` is given; see the section below
for where it sits. On an unclipped run it stays `pending` forever, the same way
`record` does on every non-meeting input.

`transcribe` and `fetch_video`→`frames` run as concurrent bash branches; both
write state through the flock'd `runstate.py`, so their writes can't clobber
each other.

**Kaltura is the one input type where they are not independent.** A YouTube
transcript comes from captions, so transcribe never needs the download; a
Kaltura entry usually has no captions, and then AssemblyAI needs the media
file. So `ensure_video_fetched` runs ahead of both branches for `kaltura`:

```
input ──> fetch_video ──┬─> transcribe ─┐
                        └─> frames ─────┴─> summarize
```

`ensure_video_fetched` is idempotent (it returns early on a `done` stage), which
is what lets the same function be called ahead of the branches here and from
inside the frames branch on the YouTube path without ever downloading twice.

### `--clip`: summarizing part of a video

Settled with the operator 2026-09-10. Four decisions, all of them deliberate:

1. **The media is cut, not the transcript filtered.** `lib/clip.py cut` runs
   ffmpeg before transcription, so AssemblyAI bills the window and not the
   lecture, and frame extraction only walks the window. Filtering afterwards
   would have been less code and would have paid full price on every clip.
2. **`--clip` is one flag for the whole invocation**, with a per-input
   `#t=WINDOW` suffix on top of it (added 2026-09-11, after the first real use
   showed why). The global flag alone could not express "these five lectures,
   two of them trimmed, one combined document" — and `--combine` is
   per-invocation, so splitting by window would have split the document too.
   `#t=` overrides `--clip` for its own input; an input without one falls back
   to `--clip`.
3. **Output timestamps are clip-relative.** Because the cut happens first,
   nothing after it knows a window existed: no offset is threaded through
   transcribe → chunking → frames → pdf, and there is no way for one stage to
   forget to apply it. The cost is that `Frame 1 @ 0:00:00` in a clipped
   summary is 00:05:00 in the source, which is why `document.py` prints a
   visible `Clip:` line as well as a provenance field. Absolute timestamps
   would have meant an offset in four modules and a silent failure in whichever
   one missed it.
4. **A clipped run has its own run id** — the window's token goes in the safe
   name (`yt_abc123_c000500-013000_20260910_143000`). Every artifact path
   derives from the run id, so this one string is what keeps a clip's `.txt`,
   `.srt`, `.md` and `.pdf` from landing on a full run's.

The stage sits wherever the media first exists:

```
local file / kaltura:  ... fetch_video ──> clip ──┬─> transcribe ─┐
                                                  └─> frames ─────┴─> summarize
youtube:               fetch_video ──> clip ──> frames
```

`ensure_video_clipped` mirrors `ensure_video_fetched` exactly — idempotent, so
it can be called ahead of both branches (local file, Kaltura: transcribe reads
the media) or from inside the frames branch (YouTube: transcribe reads captions
and would otherwise be made to wait on a download and an ffmpeg pass for
nothing).

Non-obvious details:

- **The label, not the raw spec, is what gets stored.** `pipeline.sh` parses
  `--clip` before it classifies anything — a typo must cost nothing, not a
  download and an AssemblyAI charge — and writes the canonical
  `00:05:00-01:30:00` into `state.json`. `5:00-90:00` and `300-5400` therefore
  resume the same run instead of making three runs of the same 85 minutes.
  `runstate find` matches on input **and** clip; without that, asking for
  01:30:00-02:00:00 would resume the 00:05:00-01:30:00 run.
- **The label has to parse back.** It is re-read from `state.json` on every
  attempt, including every resume, so `parse_clip(label(w)) == w` is an
  invariant, not a nicety — which is why `parse_clip` accepts the word `end`
  that `label` emits for an open window. The first version didn't, and failed
  one download in, on the resume path only.
- **The partial is `clip.part.mp4`, not `clip.mp4.part`.** ffmpeg picks its
  muxer from the output extension and refuses to start on a name ending
  `.part` ("Unable to choose an output format"). The Kaltura download's
  `.part` convention doesn't transfer, because that one is an HTTP body being
  written to a file, not ffmpeg choosing a container.
- **`-ss` before `-i`, and `-t` rather than `-to`.** Seeking before the input
  is the difference between seconds and minutes on a 90-minute lecture; and
  with `-ss` first the timestamps are already rebased, so `-to` would measure
  from the wrong origin and the clip would run long.
- **`-avoid_negative_ts make_zero` is what makes the clip start at t=0.**
  Without it the first frame is still stamped 00:05:00, every SRT cue and frame
  timestamp inherits the offset, and the clip-relative timebase this feature
  promises is silently absolute. Nothing errors.
- **Stream copy by default**; `CLIP_REENCODE=1` re-encodes. The copy lands on
  the keyframe at or before the requested start — a few seconds early here —
  and costs seconds instead of the half hour a re-encode of an 85-minute window
  takes on this box.
- **Captions have no media to cut**, so `transcribe.sh --clip-captions` applies
  the same window to the segments and shifts them onto the same clock, using
  `clip.window_segments` — the same module, so the two halves cannot disagree
  about what "00:05:00" means. The flag is named for what it does: it has no
  effect on the AssemblyAI path, whose media has already been cut, and applying
  a window there too would take a second slice out of the first.
  A straddling caption cue is truncated rather than dropped: the words were
  spoken inside the window.
- **A window that leaves no transcript is exit 2**, not an empty summary.
- **`--clip` on a live meeting URL is refused** in `pipeline.sh`, with the
  command to clip the recording afterwards. There is no source to cut.

#### The `#t=` suffix

`split_clip_suffix` in `pipeline.sh`, and three details that are easy to undo:

- **The split runs BEFORE `looks_like_input`, not after.** A URL still looks
  like a URL with the suffix attached, so testing first takes the whole string
  as the input and the window disappears without a word. (A local path is the
  opposite case — `lecture.mp4#t=1:00-2:00` is not a file that exists, so it
  would be filed as a legacy positional.) One ordering handles both.
- **The suffix is only taken as a window when what is left of it still looks
  like an input, and then it must parse.** That is what keeps a URL ending in
  some other `#t=` fragment from being silently truncated, while a typo'd
  window is a hard error at second zero rather than a mangled link.
- **`INPUTS`, `INPUT_CLIP_LABELS` and `INPUT_CLIP_TOKENS` are parallel arrays,
  read by index.** Every push site must push to all three — playlist expansion
  included, which rebuilds all three in lockstep. A missed push doesn't error;
  it shifts every later input's window onto the wrong lecture. The window is
  deliberately NOT carried inside the input string, because that string is the
  auto-resume key and the document's link line.
- **`--from-file` strips a `#` comment only at the start of a line or after
  whitespace.** It used to cut at any `#`, which ate the suffix — and any URL
  fragment — leaving a link that still worked and a window that had silently
  gone.

### Resume semantics

- Re-running the same command **resumes by default**: `runstate.py find` looks
  for a run with the same `input` whose `summarize` isn't done.
- A stage counts as done only if **every artifact it recorded still exists**.
  A state file that disagrees with the filesystem is worse than none. The
  one exception is a stage marked `cleaned` (frames after a sweep): it stays
  `done` with no artifacts, and `branch_frames` re-extracts only when this
  run is actually about to summarize.
- **Members of a `--combine` set are permanently "incomplete"** (their
  summarize never runs), so the same command always resumes them; the
  combine run is matched on its key *without* `--incomplete`, so a finished
  combined summary is reported as done rather than paid for again.
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
- `--clip-captions WINDOW` trims a *caption-derived* transcript to a window and
  rebases it, immediately before that shared writer. It is a no-op on the
  AssemblyAI path by design — see the `--clip` section above.
- `--out-base PATH` overrides the timestamped default (see the run model).
- Language default `th`, override via arg or `ASSEMBLYAI_LANGUAGE`.
- `ASSEMBLYAI_BASE_URL` and `YT_TRANSCRIPT_API_URL` exist so
  `lib/test_media_e2e.sh` can run the real clients against local stub servers.
  They are test seams, not features — but they are also the only way to
  exercise these clients without spending money, so don't remove them.

### Kaltura embeds (`lib/kaltura.py`)

Lecture-capture entries pasted out of an LMS, as either the whole `<iframe>`
tag or just its `src`. Both forms are accepted and **both must produce the same
run id** (`kal_<entry id>`), or pasting the tag once and the URL later would
duplicate the run instead of resuming it.

**Not yt-dlp.** yt-dlp ships a Kaltura extractor and it fails on these entries:
it sends no `Referer`, and a university tenant's access-control answers a
referer-less `playManifest` with a bare 404 — `No video formats found!`.
Verified 2026-09-09 against partner 2910381 / entry `1_y9jay9sw`, which
downloads fine through the api_v3 calls this module makes. Don't "simplify"
this back to yt-dlp.

**The `Referer` is the whole trick**, and it is the thing that will break for
someone else's tenant. Measured on that entry: no referer → 404,
`https://example.com/` → 404, the LMS's own domain → 302, and the *Kaltura CDN's
own domain* → 302. The CDN domain is therefore the default, because it needs no
per-institution configuration; `KALTURA_REFERER` overrides it. A 404 during the
download names that variable in the error, because nothing else about the
failure suggests a header.

The sequence: `session.startWidgetSession` for an anonymous KS (what the
embedded player itself does), then `baseEntry.getPlaybackContext` for the
sources — take the progressive `format=url` MP4, not HLS, since both downstream
stages want a file — with the KS appended to the URL, which access-control also
requires. `baseEntry.get` supplies the title, since there is no yt-dlp to ask.

Non-obvious details:

- **`requests` is imported lazily.** `pipeline.sh` runs `kaltura.py parse` on
  every input it classifies; an `ImportError` there would silently reclassify a
  perfectly good embed as "unrecognized input" rather than failing loudly.
  `test_parse_does_not_even_import_requests` holds it.
- **Kaltura reports failures inside a 200 body** (`KalturaAPIException`), so the
  status code proves nothing — same shape as the claude CLI's exit code.
- **Captions are tried before AssemblyAI**, exactly like the YouTube path, and
  "no usable track" is exit code **3**, not 1: `transcribe.sh` reads 3 as
  "fall through to the media file" and anything else as a real failure. Collapse
  them and an outage becomes three silent uploads to AssemblyAI. Only SRT and
  WebVTT assets are used; a DFXP/TTML track is skipped rather than half-parsed,
  because a mangled transcript is worse than paying for a good one.
- **The download writes `<dest>.part` and renames**, and a transfer that ends
  short of `Content-Length` is an error rather than a short file. An
  interrupted download must never look like a finished artifact to the resume
  logic, and a truncated MP4 would otherwise only fail two stages later, in
  ffmpeg.
- **Every request is retried through `summarize/retry.py`**, not a local copy —
  the project's backoff policy (exponential, full jitter, `Retry-After`) is
  documented and shouldn't drift. Transient statuses are re-raised as
  `HTTPError` so `is_retryable` classifies on the status rather than on the
  wording of a `KalturaError`. This was added 2026-09-09 after the first live
  run on the deployment box died on a single 60s read timeout on
  `getPlaybackContext`, seconds after the same host had answered `baseEntry.get`
  fine. It is the one place `lib/` imports from `summarize/`; that import is
  lazy, so the offline `parse` path is unaffected.
- **The `<iframe>` blob never reaches summarize.** `run_one.sh` normalises it to
  a canonical embed URL (`SOURCE_URL`) first; otherwise 900 characters of HTML
  would land in the document's provenance comment and its link line.
  `document.py` prints that as `Video Link:` under a `kaltura` source kind.
- Entry facts are cached in `runs/<id>/kaltura.json` at fetch time, so summarize
  makes no network call of its own and a resume makes none either.

**Measured on the deployment box, 2026-09-09**, on the entry above (1h29m,
1920x1080, no captions), as a live `./verify_e2e.sh --kaltura` run:

| Stage | Wall | Note |
|---|---|---|
| `fetch_video` | ~10s | 446MB, ~45MB/s from the CDN |
| `transcribe` | ~3 min | AssemblyAI, 132,488 Thai chars in **19** cues |
| `frames` | ~25 min | 149 keyframes; slower than the 13x-realtime benchmark because transcribe and summarize were competing for the same 4 vCPU |
| `summarize` | ~9 min | claude-cli/opus, chunked |

Two things that table is worth keeping for: the transcript arrives as 19 cues
for 89 minutes, so this input type leans hard on
`chunking.split_long_segments` (the AssemblyAI-Thai problem documented under
Stage 3), and the 446MB download stays in the run dir after the frames are
swept — it is the resume's cheap path back to frames, and the disk cost of a
Kaltura run is therefore the video, not the frames.

**An entry that needs a real LMS login fails loudly** — `getPlaybackContext`
returns no sources, and the error names the partner and entry id. Browser
recording it is deliberately NOT implemented: the login lives on the LMS page,
not on the iframe src, so opening the embed in the persistent Chrome profile
would hit the same access-control refusal. Settled with the operator
2026-09-09; if it is ever built, the decision taken then was to reuse
`record_screen.sh` as-is (a Kaltura-specific capture driver in place of the
Meet/Zoom join logic), taking a `record` queue slot, and to require the *LMS
page* URL rather than the iframe src.

### Stage 3 — Summarize (`summarize/`)

Split across seven modules:

- `summarize.py` — entry point and orchestration.
- `llm_client.py` — backend dispatch + the fallback chain.
- `retry.py` — transient-failure policy (503/429/5xx).
- `chunking.py` — splitting long transcripts, assigning frames to chunks.
- `mapreduce.py` — parallel chunk summarization + the merge call.
- `document.py` — the course-note document wrapper (single and multi-video).
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
claude -p --output-format stream-json --verbose --model <CLAUDE_CLI_MODEL> --effort <SUMMARY_EFFORT>
       --safe-mode --no-session-persistence
       [--append-system-prompt-file <sha>.md --exclude-dynamic-system-prompt-sections]
       --input-format stream-json --tools ""            (frames inline; the default)
     | --tools Read --allowedTools Read --add-dir <dir>  (CLAUDE_CLI_FRAME_INLINE=0)
     | --tools ""                                        (no frames / vision off)
```

- **The prompt goes in on stdin, never in argv.** An 80KB transcript in an
  argument is over `ARG_MAX` on any normal box.
- **`--effort` is where `SUMMARY_EFFORT` lands.** Same scale the Messages API
  spells `output_config.effort`. There is no token-budget knob and no CLI
  spelling for one, which is a more durable fix than remembering not to send
  `budget_tokens`. **`SUMMARY_MAX_TOKENS` is Gemini-only and always was**;
  `_max_tokens()` is called from `summarize_gemini` and nowhere else. The
  operator set it to 100000 in 2026-09 expecting it to bound the
  subscription spend and saw no change, which is what led to the usage
  window section below.
- **`--output-format stream-json --verbose`, not `json`.** The result
  envelope is the same object, arriving as the last line; what the streaming
  format adds is a `rate_limit_event` line carrying the subscription's own
  meters (`rate_limit_info.unifiedWindows.five_hour.{utilization, resetsAt}`)
  and, on an exhausted window, `status: "rejected"` plus the reset time. That
  event is the only machine-readable form of either fact. `--verbose` is
  what print mode requires before it will stream. Verified 2026-09-12 on
  v2.1.263 with two Haiku calls; `parse_cli_output` still accepts a bare
  envelope so the unit tests and an older stub keep working. The cost is
  that the tool results (the frame images the model Reads) are echoed into
  stdout as base64 — a few MB per chunk, held in memory once, discarded.
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

**`CLAUDE_CLI_BIN` should be set, not left to `PATH`.** The CLI installs into
`~/.local/bin`, which root's minimal `.profile` does not add to `PATH` and a
systemd unit does not inherit. `_claude_cli_bin()` returning `None` is not an
error anyone sees: it raises `BackendUnavailable`, the chain advances, and
Gemini answers. Found 2026-09-08 on the deployment box with the variable
commented out in `.env` — 51 summaries had been billed to Gemini keys while
the operator believed the subscription was paying. The symptom is in every
document's provenance header (`model: gemini/...`) and nowhere else.

**The subprocess environment is scrubbed** (`_claude_cli_env`): `ANTHROPIC_API_KEY`,
`ANTHROPIC_API_KEY_1`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are
removed before launch. This is the most important line in the file. If any of
them survives, the CLI switches from subscription auth to API-key billing and
*says nothing* — the summary is identical and the charge lands on an account
the operator thought was unused. An empty `ANTHROPIC_API_KEY` is worse: it fails
auth in a way that reads like a broken subscription. `lib/fake_claude_cli.py`
records what reached the child process precisely so this regression is visible.

**Frames are inlined as image blocks, in one turn** (settled 2026-09-12,
the token-usage pass). `--input-format stream-json` makes stdin a JSON user
message whose content is blocks, and the CLI accepts `image` blocks there
exactly as the Messages API does — verified on v2.1.263 with Haiku
(`build_inline_input`). The prompt text goes first, then for each offered
frame its manifest label and the image, so the number beside the picture is
the number the model cites. `--tools ""`: nothing to open.

The previous delivery — absolute paths in the manifest, `--tools Read
--allowedTools Read --add-dir <frame dir>`, the model Reads each file — is
kept behind `CLAUDE_CLI_FRAME_INLINE=0` for a CLI too old to take stream-json
input, and for nothing else. It was replaced because **each Read is a turn,
and each turn re-sends the whole context**: `usage.iterations` in the
envelope showed it, and on a 12-frame chunk over a ~30k context that is
300k+ cache-read tokens — more than the frames themselves cost. The ledger's
`cache_read_input_tokens` against `input_tokens` is the tell in any old
`state.json`. `CLAUDE_CLI_FRAME_VISION=0` still sends the manifest as text
only, and then the model cites frames it has never seen.

**Frames are filtered before they are offered** (`drop_uninformative`, same
pass). Two kinds cost tokens and teach nothing: blank frames — the PDF already
drops them (`framecrop.is_blank`) but the model was still being shown them,
and the scene-change pass *prefers* them — and consecutive periodic samples of
a slide that has not changed. The repeat test is `framecrop.frame_hash`: a
**texture** hash (per-cell pixel spread, 64x64 cells) over the *slide region*
found by `detect_crop`, so a participant filmstrip beside the slide never
enters it. Measured on synthetic 1920x1080 frames: a moved cursor flips 2 of
4096 bits, a shade change 5, a changed title 58, changed body text 176;
`FRAME_DEDUPE_MAX_DISTANCE` is 16. Only *consecutive* repeats go — a slide
returned to later is a moment the notes may cite — and a frame is never
matched against one from another video of a `--combine` set. Numbers are
untouched; the PDF resolves every citation as before. Two hashes were tried
and rejected first, and the reasons matter if anyone revisits this: a
difference hash encodes only the *sign* of the gradient between neighbours,
so a text line that grew or shrank left it unchanged; an average hash
compares cells against the frame's mean, which the dark chrome drags so low
that every cell on a white slide reads "bright" whether it holds text or not
— it deduplicated two slides with different titles in the live check.
`CLAUDE_CLI_FRAME_DEDUPE=0` offers everything.

The order is fixed: drop blanks and repeats, *then* `thin_frames` to the cap,
*then* crop and downscale. Thinning first would spend the cap on twelve
copies of one slide. `test_dedupe_runs_before_the_cap` holds it.

**The static half of the prompt is a system prompt file, so the prefix can
cache.** Claude's prompt caching keys on a byte-identical prefix; the CLI has
no `cache_control` flag and no caching flag of any kind (checked against
`claude -p --help`, v2.1.259) — caching is automatic, and the only lever we
have is keeping the prefix still. Before this, nothing was still: `mapreduce`
prepends the part label ("Part 2 of 5, 0:12:00-0:24:00") to the *front* of the
template, so three parallel chunks of one lecture shared no prefix at all, and
the CLI's own system prompt carries the date and cwd, which move on their own.

A prompt template may now fence the half that never varies between runs with

```
<!-- static-prompt: begin -->   ... role, instructions, output format, example
<!-- static-prompt: end -->
```

`llm_client.split_static_prompt` lifts that block out, writes it to a
**content-addressed** file (`$MEETING_BOT_ROOT/tmp/claude-cli-prompts/<sha>.md`
— same instructions, same path, same bytes), and passes
`--append-system-prompt-file` plus `--exclude-dynamic-system-prompt-sections`,
which moves the CLI's cwd/env/date/git sections out of the system prompt and
into the first user message. Everything that varies — the chunk label, the
reference material, the transcript, the frame paths — stays in the piped user
turn. Both flags exist but are undocumented in `--help`; they were verified by
invocation (an unknown flag errors immediately, these don't).

The split is **opt-in per prompt file**. `prompts/summarize-v2.md` and
`prompts/lecture-claude.md` (the configured default) carry the markers; every
other template splits to `(None, itself)` and is sent exactly as it always was.
`CLAUDE_CLI_STATIC_PROMPT=0` turns the whole thing off for a CLI too old to
know the flags. The markers are stripped in `_render` so they never reach any
model, gemini included.

For `lecture-claude.md` the split is 5,473 static characters against 346
dynamic ones, so on a chunked lecture every chunk after the first reuses the
whole instruction set.

Note the trap this design avoids: if the varying part label ended up inside the
static block, every chunk would write a *different* system prompt file, the
cache would never hit, and **nothing would look wrong** — the summaries would
be identical. `test_a_prepended_chunk_label_stays_dynamic` is what holds it.

**`load_prompt_template` cuts a template at its first `# Input`** and returns
only the tail — unless the static-prompt markers are present, in which case the
file is returned whole. That legacy path is a trap, because the cut lands on
the first *substring* match rather than on a heading:

- `prompts/summarize.md` (the unused fallback default) loses its entire
  role/format/rules section — its first match is the real `# Input` at line 46.
- `prompts/lecture-claude.md` used to lose its opening role sentence and start
  the prompt with the orphaned word `Data`, because its first match was the
  *"# Input Data"* heading near the top. Adding the static-prompt markers
  fixed that as a side effect: the marker path returns the file whole, so
  "You are an expert academic tutor and note-taker…" now actually reaches the
  model for the first time. Verified 2026-09-08 by diffing what the old split
  produced against the new one.

Prefer the markers over relying on the cut. If you write a new prompt file
without them, check what `load_prompt_template` actually returns.

**Frames sent to the CLI are cropped to the slide and downscaled; the saved
frames are not.** `framecrop.fit_for_llm` runs `detect_crop` in the PDF's own
`PDF_FRAME_CROP` mode (one knob, deliberately: the model looks at the picture
the PDF prints, and the detector's decline-rather-than-guess rule protects
both) and then fits the result to `FRAME_MAX_DIMENSION` (default **768** since
2026-09-12, was 1024) on the long edge. Cropping first is what makes 768
enough: on a Meet recording the slide is maybe two thirds of the frame, and
the dark chrome was paying for pixels that said nothing. Live check with
Haiku: 60px slide titles read correctly off the 768px cropped copy. A whole
1920x1080 frame is ~1,844 tokens, ~790 at 1024px, ~440 at 768px, and the crop
takes it lower still. The copy goes to `<frame dir>/llm-<px>-<mode>/<same
name>.jpg` — the mode is in the directory name so flipping `PDF_FRAME_CROP`
doesn't reuse copies cut the old way — and is reused on the next run. The
originals never move, because `pdf.py` crops and embeds them and needs the
resolution — `_downscale_frames` builds new `FrameMeta` objects with
`dataclasses.replace` rather than touching the ones `summarize.py` passes on to
the PDF. Pillow is optional: without it the originals are sent with one
warning. `SCENE_THRESHOLD` and `FRAME_PERIOD_SECONDS` are untouched — this
changes resolution, never which frames exist.

**The chunk label is appended, not prepended, and the reference material sits
at the top of the dynamic half.** Both in the same pass. Caching keys on a
prefix, and the label is the one thing that differs between the chunks of a
run; in front of everything it meant the `--resources` block behind it —
identical on every chunk, up to `RESOURCE_MAX_CHARS` = 40k characters — never
cached. Now `inject_resources` inserts the block right after the static-prompt
end marker (a template without markers is appended to, as before, so its
instructions still come first) and `mapreduce` puts the label after the frame
manifest. `PromptOrderForCachingTest` holds both. In the inline-image message
the text block comes before the images for the same reason: the images differ
on every chunk.

**The merge call may run on a cheaper model.** `mapreduce` passes
`role="merge"` through `summarize` → `summarize_with_fallback` → the backend;
`summarize_claude_cli` swaps in `CLAUDE_CLI_MERGE_MODEL` / `SUMMARY_MERGE_EFFORT`
for that call only (default: the chunk's model and effort, so an existing
`.env` changes nothing). The merge reads every partial and — by `_merge.md`'s
own rule, "do not compress" — writes them all out again, so its output is
about the size of everything the chunks produced; output tokens are the
expensive kind, and this was the single most expensive call of a chunked run.
The chunk summaries, where the reading of noisy Thai ASR happens, stay on
Opus. `.env.example` recommends `sonnet` / `low` for the merge and
`SUMMARY_CHUNK_CHARS=60000`, so most lectures under ~90 minutes are one call
and have no merge at all; the code defaults (24000, same model) are
unchanged. `gemini` accepts and ignores `role`.

### The usage window: metered, waited for, never handed to Gemini

Settled with the operator 2026-09-12. A Pro/Max subscription has a rolling
5-hour window and a 7-day one; before this, running out looked like *"claude
CLI is not logged in"* (the CLI's own error classifier files "usage limit
reached" beside the auth errors) and the chain quietly handed the summary to
Gemini. Four decisions:

1. **Every call's usage is recorded, with the meter.** `UsageLedger`
   (`llm_client.USAGE`) takes the envelope's `usage` (input, output, cache
   read/creation, thinking), `total_cost_usd` (list price — a decent proxy
   for how much of the window a call took), and the `rate_limit_event`'s
   `unifiedWindows`. `summarize.py` writes `USAGE.summary()` to
   `stages.summarize.usage` in `state.json` on *every* exit path — the calls
   that completed before a failure were spent too. `utilization_delta` on
   `five_hour` is "what fraction of the window this stage cost", which is
   the number the operator asked for and the only honest way to size a
   lecture to a plan. Per-call lines go to the stage log.
2. **A hit window waits in-process, on Claude.** `_rate_limit_from` turns a
   rejected event (or `api_error_status` 429, or the legacy
   `usage limit reached|<epoch>` wording) into `ClaudeCliRateLimited`, which
   is `retryable = False` — it must not burn the backoff schedule — and
   which `summarize_with_fallback` re-raises instead of advancing to Gemini.
   `summarize_claude_cli` loops: `_wait_for_window` sleeps until the reset
   the CLI named plus `RATE_LIMIT_MARGIN_SECONDS` (60), or polls every
   `CLAUDE_CLI_RATE_LIMIT_POLL_SECONDS` when no time was given, bounded in
   total by `CLAUDE_CLI_MAX_WAIT_SECONDS` (6h — a full 5h window plus margin;
   anything longer is the weekly limit and no stage should sit through
   that). Sleeps go through `llm_client._sleep` so the tests can patch the
   clock. `WAIT_HOOK` mirrors the wait into `stages.summarize.waiting_until`
   so `--status` can distinguish "waiting" from "hung".
3. **Past the cap, the stage pauses rather than merging around the hole.**
   `mapreduce` re-raises a chunk error carrying `pause_run = True` instead
   of writing "*(this part could not be summarized)*" and marking the stage
   done — nothing would ever fill that gap. `summarize.py` exits **75**
   (`EX_TEMPFAIL`) with `stages.summarize.rate_limited = {window, resets_at,
   resets_at_iso}` annotated; `run_one.sh` marks the stage failed as usual
   and passes 75 up; `pipeline.sh` reports `PAUSED` (not `FAIL`) and exits
   75. `runstate.start` and `done` clear the field — a stale reset time
   would make the next point skip a run that could go.
4. **`--resume-all` is the resume, and a timer is its backstop.** It skips a
   run whose `rate_limited.resets_at` is still in the future (and one whose
   `run.lock` owner is alive), so firing it every 15 minutes never retries
   into the same wall. `meeting-bot-resume.{service,timer}` do exactly that
   (`setup.sh --with-resume-timer`, `SuccessExitStatus=75`,
   `OnBootSec=5min` so a reboot mid-wait recovers on its own). The in-process
   wait is the primary path; the timer exists for the process being gone.

`CLAUDE_CLI_MAX_FRAMES` came out of the same conversation: frames are the
bulk of a call's input, so `thin_frames` caps what the model is *offered* —
scene changes first, periodic frames at an even stride so the cap still
covers the whole window. It runs after the blank/duplicate pass, so the cap
counts distinct slides. Numbers are untouched (they were assigned over the
whole manifest), so the PDF resolves whatever the model cites. Code default 0
(= off): turning it on by default would silently change every existing run's
summaries. **The recommended Pro-plan values live in `.env.example` instead**
(settled with the operator 2026-09-12): `CLAUDE_CLI_MAX_FRAMES=20` (raised
from 12 the same day — with duplicates gone and each frame a third of its old
cost, 20 distinct slides per chunk is cheaper than 12 samples were),
`FRAME_PERIOD_SECONDS=60`, `SUMMARY_EFFORT=medium`, `SUMMARY_MAX_PARALLEL=1`,
`SUMMARY_CHUNK_CHARS=60000`, `CLAUDE_CLI_MERGE_MODEL=sonnet`,
`SUMMARY_MERGE_EFFORT=low`. The README's table still lists the code defaults;
the two differing on purpose is documented there.

The fixed overhead the CLI itself adds was measured while sizing this
(v2.1.263, Haiku, trivial prompt): ~4.9k tokens of system prompt with the
Read tool, ~3.5k with `--tools ""`, cached as `ephemeral_1h` automatically.
Small next to a chunk; not a lever worth chasing.

markitdown (microsoft/markitdown) was evaluated the same day as a way to cut
resource tokens and **rejected**: it converts to Markdown, which carries more
markup than the words-only extraction `resources.py` already does, and its
image path *spends* an LLM call per picture. Its real benefit is structure
(tables, headings) for slides; the operator chose not to take on pdfminer +
python-pptx + mammoth + magika for that. Don't re-propose it as a token saver.

**The CLI exits 0 when it is not logged in.** The only signal is the body:
`is_error: true` with `result: "Not logged in · Please run /login"`. So
`_run_claude_cli` parses the JSON rather than trusting the status, and maps
auth wording to `BackendUnavailable` so the chain advances to Gemini
immediately. The marker list has to keep up with the CLI's wording: an expired
OAuth session says *"Failed to authenticate: OAuth session expired and could
not be refreshed"*, which matched none of the original markers and so burned
the full retry schedule before falling through. `failed to authenticate` and
`oauth session expired` were added 2026-09-08 after seeing it live. Everything else becomes `ClaudeCliError` carrying the CLI's own
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

**Segment granularity.** A segment is the atom for both chunk boundaries and
frame windows, so `chunking.split_long_segments` cuts any segment over
`SUMMARY_SEGMENT_MAX_SECONDS` (120) or `SUMMARY_SEGMENT_MAX_CHARS` (2000) into
pieces with linearly interpolated timestamps, before chunking. Segments under
the caps are returned untouched, so a normal transcript behaves exactly as it
did.

This exists because **AssemblyAI returns almost no sentence boundaries for
Thai**: Week01, a 2.6-hour lecture, came back as *three* cues, the first a
single 29-minute "sentence". A segment can't be split by `chunk_by_segments`,
so chunks came out at 60k and 70k characters against a 40k limit, chunk 1's
window ran 0-6966s while chunk 0's was 0-1782s, and every frame in a half-hour
had an equal claim on every sentence in it. After the split: 3 segments become
79, the longest drops from 5184s to 127s, chunks land at 39.6k/39.3k/8.7k, and
the windows are sequential. Interpolating on character offset assumes an even
speaking rate — wrong in detail, and enormously closer than one 29-minute atom.

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
- `--combine` produces one such document for several videos: one title, one
  link line per video tagged `(Video N)`, one transcript block, one body. See
  the `--combine` section below.

### `--combine`: several videos summarized as ONE (`--combine-pdf`, `--no-combine-pdf`)

Reworked 2026-09-11 with the operator. Until then `--combine` summarized every
input on its own and stapled the `.md` files together, renumbering frame
citations per section so the combined PDF's pictures lined up. That whole
mechanism — `document.combine_documents`, `shift_frame_citations`,
`pdf.merge_manifests` and the offsets they passed around — is **gone**. Don't
bring it back as a "concat mode": the operator chose to replace it, not to
keep both.

What it does now: the members run transcribe + frames and stop
(`run_one.sh --skip-summarize`), and a **combine run** — `input_type:
combine`, run id `combine_<n>x_<sha1[:10] of the member ids>_<time>` — runs
one summarize stage over all of them via `summarize.py --parts parts.json`.
The model reads every transcript, in input order, and writes one body.
Decisions, each settled explicitly:

1. **One summary, not a merge of per-video summaries.** N+1 calls would have
   been cheaper to build and weaker across video boundaries; one summarize
   spend is also the point — the members deliberately get **no individual
   summary** (their `summarize` stays `pending`, and `combined_into` in their
   state says why).
2. **Per-video clocks, never a running total.** The transcript the model
   reads is fenced per video (`chunking.part_transcript`:
   `=== video 2 of 3: <title> ===`), every frame label carries its video
   (`[frame 12 @ video 2 410.0s (periodic)]` — `FrameMeta.part`), chunk
   headers name the video, the PDF caption reads `Frame 12 — Video 2, 6:50`,
   and the document says under its links that timestamps are relative to the
   video they cite. A continuous clock would have meant an offset in four
   modules and a silent failure wherever one was missed — the same reasoning
   as `--clip`'s relative timestamps, one level up.
3. **Frame numbers are global across the set, assigned once.**
   `pdf.load_part_manifests` tags each manifest's frames with its part and
   runs `assign_numbers` over the lot, which now sorts by
   `FrameMeta.sort_key = (part, timestamp_s)`. A video with no manifest still
   consumes a part number. This is the multi-video form of "Frame numbers are
   global, and assigned exactly once" below, and the failure mode is the same
   silent one.
4. **Each video is chunked on its own** (`chunking.build_part_chunks`): a
   chunk never spans two videos, because its time window and its frames
   belong to one clock, and a short video is never folded into a neighbour's
   chunk because the chunk label is what the model cites frames against. If
   the whole set fits under `SUMMARY_CHUNK_CHARS`, it goes in one call with
   every frame — that is the case where cross-video context actually helps.
5. **The combine run is a real run**, resumable through the same
   `runstate find --input` auto-resume as everything else: its `input` is
   `combine:<id1>+<id2>+…` (`lib/combine.py run-key`), so the same members in
   the same order land on the same run. Its `members`, `output_md` and
   `output_pdf` live in `state.json` and `init` refreshes them on every
   invocation, so re-running with a different `--combine` path writes there.
6. **Flat wrapper**: one title (the first video's, or `--title`), one link
   line per video tagged `(Video N)`, one `<details>` block holding the fenced
   transcript, one body. `document.build_document(videos=[...])`.

Non-obvious details:

- **`run_one.sh` handles the combine run before the per-input DAG** (the
  `INPUT_TYPE = combine` branch). Before summarizing it checks every member
  has its transcript and manifest on disk; a member whose frames were swept
  (`done` with no artifacts — the `cleaned` state) gets `rs reset --stage
  frames` and is re-run with `--skip-summarize`. That is what makes
  `--run-id combine_… --force` work after the sweep, and it is why
  `parts.json` is rebuilt on every attempt rather than cached.
- **The frame sweep belongs to the combine run** (`cleanup_member_frames`),
  after its PDF, under the same rules as a single run. `pipeline.sh` no
  longer exports `KEEP_FRAMES=1` to the children or sweeps anything — members
  never reach `cleanup_frames` under `--skip-summarize`.
- **`--resume-all` skips members** (anything with `combined_into` set) and
  resumes the combine run instead. Without that it would bill an individual
  summary nobody asked for. `--run-id <member>` still does run one to the
  end — that is an explicit ask.
- **`--combine` with `--run-id`/`--resume-last`/`--resume-all` is refused**;
  the combine run is resolved from the inputs.
- **A failed member leaves the combine run untouched** — no summarize is
  attempted, exit 1, and the same command resumes both.
- **`summarize.py --parts` looks up YouTube titles itself** (one yt-dlp call
  per video, best-effort) because the labels the model reads need them;
  Kaltura titles come from each member's `kaltura.json` through
  `combine.py parts`. The `<iframe>` blob is normalised there too.
- **`pdf.py`'s CLI takes repeated `--frames-manifest`** to re-render a
  combined document: the Nth is video N's, `-` holds an empty slot.

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

### Frame numbers are global, and assigned exactly once

`llm_client.assign_numbers()` numbers every frame by its position in the whole
recording, and `summarize.load_manifest()` is the only caller — because it is
the only place that sees the entire manifest. Everything downstream gets
slices.

This is not a style preference. `_render()` numbers whatever list it is handed
and is called **once per chunk**, so before the fix chunk 3's fifth frame was
announced to the model as "frame 5" and so was chunk 1's. The model cited what
it was shown, faithfully; `pdf.py` numbers across the whole recording, so it
resolved half the citations to a picture of a completely different moment —
which is what the operator saw as "the image is completely unrelated to the
content". The tell, in a finished summary, is the same frame number carrying
two timestamps:

```
$ grep -o 'Frame 4 @ [0-9:]*' Week01.md
Frame 4 @ 0:03:39      <- chunk 1's fourth frame
Frame 4 @ 1:31:24      <- chunk 2's fourth frame
```

Found 2026-09-08 in `Week01_20260907_230838.md`: 10 of 40 cited numbers named
two different moments. `_render` still falls back to the position in its list
when a frame has no number, which is only correct when that list is the whole
manifest — that fallback exists for direct callers and the unit tests, not for
the pipeline.

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
- **Kaltura is reached through `lib/kaltura.py`, never yt-dlp.** Its extractor
  sends no `Referer` and 404s on exactly the entries this project exists for.
- **Every Kaltura request carries a `Referer`.** Dropping it turns a working
  entry into a bare 404 with nothing to explain it. The default is the CDN's own
  domain so no tenant-specific configuration is needed; `KALTURA_REFERER`
  overrides it.
- **`kaltura.py parse` stays offline and dependency-free.** `classify_input`
  runs it on every pipeline input; an import error or a network call there
  breaks classification for inputs that have nothing to do with Kaltura.
- **"No captions" is exit code 3, distinct from failure.** `transcribe.sh`
  routes on it. Merging it into 1 turns a Kaltura outage into three paid
  AssemblyAI uploads of a video whose captions were fine.
- **`fetch_video` runs before both branches on the Kaltura path.** Its
  transcribe branch needs the media file; leaving the download inside the frames
  branch races it.
- **The pasted `<iframe>` is normalised before it reaches summarize.** The raw
  tag in the provenance comment and the link line is not a document.
- **`summarize.py` does not download the video when `--frames-manifest` is
  given.** The video exists only to produce frames; once a manifest exists
  there's nothing to download. The pipeline always passes one, so re-adding the
  download would make every YouTube run fetch the same video twice.
- **Artifact paths derive from the run id, not the clock.** Resume depends on
  it. This is why `--out-base` and `--pdf-out` exist.
- **The `#t=` suffix never reaches the stored input.** It is the auto-resume
  key and the document's link line; a window left inside it breaks both.
- **A clipped run gets its own run id.** Dropping the window from the run id
  makes a clip overwrite the full summary of the same lecture, and two windows
  overwrite each other — silently, because every artifact path is a function of
  the run id and nothing checks what is already there.
- **`--clip` cuts the media; it does not filter the transcript afterwards.**
  Filtering pays AssemblyAI for the whole video on every clip. The one
  exception is captions, which have no media to cut and are free anyway.
- **Clip timestamps are relative, and the document says so.** The `Clip:` line
  in `document.py` is not decoration: without it every timestamp in a clipped
  summary points at the wrong moment of the source video and looks correct.
- **`clip.label()` must round-trip through `clip.parse_clip()`.** The label is
  what `state.json` stores and what every resume parses back. A label that
  doesn't parse fails one download in, on the resume path only.
- **`-avoid_negative_ts make_zero` stays in the ffmpeg cut**, and `-ss` stays
  before `-i`. Dropping the first makes the clip-relative timebase silently
  absolute; moving the second turns a few seconds of seeking into minutes of
  decoding.
- **The clip's partial file keeps the destination extension**
  (`clip.part.mp4`). ffmpeg chooses its muxer from the extension and refuses to
  start without one.
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
- **`CLAUDE_CLI_BIN` is set to an absolute path in `.env`.** Relying on `PATH`
  makes a signed-in, working CLI invisible to the pipeline, and the only
  evidence is which model the provenance header names.
- **`ANTHROPIC_*` must stay scrubbed from the CLI's environment.** See the
  summarize section: leaving one set redirects billing silently, and an empty
  one breaks auth in a way that looks like a broken subscription.
- **The CLI runs with `--safe-mode` in a scratch cwd.** Otherwise this file
  gets loaded into the context of every summary.
- **Nothing that varies per run may enter the static prompt block.** The chunk
  label, the reference material, the transcript and the frame paths all belong
  in the piped user turn. A leak costs the cache and reports nothing.
- **The static-prompt markers are opt-in and stripped before send.** A template
  without them must be sent byte-for-byte as it was before the split existed.
- **`FRAME_MAX_DIMENSION` downscales a copy, never the saved frame.** `pdf.py`
  crops and embeds the original; overwriting it degrades every PDF and is not
  recoverable without re-running ffmpeg.
- **Frames go to the CLI as image blocks in one turn, not as paths to Read.**
  The Read path re-sends the whole context once per frame opened. It stays
  only as `CLAUDE_CLI_FRAME_INLINE=0` for an old CLI; don't make it the default
  again.
- **Blank and repeated frames are dropped before the cap, and the cap before
  the crop.** Reordering spends the cap on copies of one slide. The repeat
  test is a texture hash over the slide region — not a difference hash (blind
  to a line that grew) and not an average hash (blind to text on a white slide
  next to dark chrome); both were tried and both merged distinct slides.
- **The chunk label goes after the frame manifest; the reference material goes
  right after the static-prompt end marker.** Anything constant across a run's
  chunks must precede anything that varies, or it never caches — and nothing
  reports the miss.
- **`role="merge"` reaches every backend's signature.** A backend that doesn't
  take it breaks the fallback chain on the merge call with a TypeError.
- **The CLI's JSON body decides success, not its exit code.** It exits 0 when
  signed out. Parsing the envelope is the only way to tell.
- **`--output-format stream-json --verbose` stays.** `json` drops the
  `rate_limit_event`, and with it the meter and the reset time; the window
  then goes back to looking like a login failure.
- **`SUMMARY_MAX_TOKENS` is not a Claude lever, and must not be wired to
  one.** There is no output cap on the CLI, and output is not what spends
  the window. The levers are `SUMMARY_CHUNK_CHARS` (whether there is a merge),
  `CLAUDE_CLI_MERGE_MODEL`, `CLAUDE_CLI_MAX_FRAMES`, `FRAME_PERIOD_SECONDS`,
  `FRAME_MAX_DIMENSION`, `SUMMARY_EFFORT` and `CLAUDE_CLI_MODEL`.
- **`ClaudeCliRateLimited` is `retryable = False` and the chain re-raises
  it.** Retrying it burns the backoff schedule; advancing hands the summary to
  Gemini, which the operator chose not to pay for. It waits, or it pauses.
- **A paused chunk fails the stage; it is never merged around.** `pause_run`
  in `mapreduce.py`. A document with "*this part could not be summarized*"
  and a `done` stage is a hole nothing will ever fill.
- **Exit 75 means paused, and `--resume-all` honours `rate_limited.resets_at`.**
  Collapsing 75 into 1 turns a timer-driven resume into a call against the
  same wall every fifteen minutes.
- **`runstate.start` clears `rate_limited` and `waiting_until`.** Otherwise a
  run that resumed and succeeded still looks paused to the next `--resume-all`.
- **`CLAUDE_CLI_MAX_FRAMES` thins what is offered, never renumbers.** The
  numbers come from `assign_numbers()` over the whole manifest; a thinned
  list that renumbered would recreate the per-chunk numbering bug above.
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
- **`--combine` is one summary over every video, not a concatenation of
  per-video summaries.** The members' `summarize` stage stays pending on
  purpose; giving them individual summaries doubles the spend. See the
  `--combine` section.
- **Timestamps in a combined document are per video, and every place a
  timestamp appears names the video.** The transcript fences, the frame
  labels, the chunk headers, the PDF captions. Dropping the video from any
  one of them makes "410.0s" ambiguous with nothing to show for it.
- **Frames of a combined set are numbered once, across all videos, in
  `(part, timestamp)` order.** Numbering per manifest gives two videos the
  same "Frame 4" and the PDF resolves both to the first.
- **A chunk never spans two videos.** Its window and its frames belong to
  one clock.
- **The combine run owns the members' frame sweep**, after its PDF.
  `run_one.sh --skip-summarize` never sweeps, and `pipeline.sh` no longer
  does either.
- **`--resume-all` must skip runs with `combined_into` set.**
- **Frame numbers come from `assign_numbers()` over the whole manifest, never
  from a per-chunk enumeration.** See the section above: the failure mode is
  silent, survives every unit test that looks at one chunk, and produces a PDF
  full of confidently mislabelled pictures.
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
| `lib/test_runstate.py` | state transitions, stale artifacts, concurrent writes, CLI, `annotate` and the pause fields | 19 |
| `lib/test_slotqueue.py` | FIFO order, dead-holder reclaim, timeout, CLI | 23 |
| `lib/test_keyring.py` | numbered slots, gaps, duplicates, cursor persistence | 22 |
| `lib/test_resources.py` | spec parsing, text extraction, GitHub fetch, budgets | 27 |
| `lib/test_kaltura.py` | iframe/URL parsing, the Referer, the KS, caption selection, download, retries | 51 |
| `lib/test_clip.py` | window parsing, the label round-trip, the ffmpeg invocation, caption windowing | 33 |
| `summarize/test_summarize_units.py` | retry classification/backoff, chunking, segment granularity, map-reduce, global frame numbering, document, the multi-video wrapper and per-video chunking for `--combine`, the claude-cli command line + envelope parsing (plain and stream-json), inline image blocks vs the Read path, the merge role, the cacheable static prompt and the label/resources order, frame crop + downscale, blank/duplicate dropping and the texture hash, the usage ledger, the hit-window wait/pause and the chain not advancing, frame thinning | 161 |
| `summarize/test_pdf_units.py` | crop geometry, citation rewriting, blank-frame detection, LaTeX extraction/fallback, the hidden transcript, part-tagged manifests and captions for `--combine`, real PDF render | 60 |
| `transcribe/test_yt_transcript_client.py` | key rotation, retry, and the `tracks[]` response shape | 16 |
| `lib/test_pipeline_e2e.sh` | full orchestration with stubbed stages, output dirs, PDF/markdown toggles, `--resources`, the combine run (members skip summarize, parts.json in input order, resume, `--force` re-extraction, failed member, `--resume-all`, the frame sweep), the Kaltura DAG, the `--clip` DAG and run-id separation, the per-input `#t=` suffix, a summarize paused on the usage window (exit 75, `PAUSED`, `--resume-all` skipping until the reset, then finishing) | 281 |
| `lib/test_media_e2e.sh` | real MP4 + real SDKs against local stub servers, the real llm_client against a stub `claude` binary (single run and `--parts`), the usage ledger landing in state.json, a hit window waited out then retried against the stub (`rate-limited-once`), a pause past the cap (exit 75, reset time recorded, Gemini untouched), and a real ffmpeg clip probed for duration and rebased timestamps | 118 |
| `verify_e2e.sh --browser-smoke` | real Chrome under Xvfb, recorded and measured for black edges | 6 |

`test_pipeline_e2e.sh` runs the real `pipeline.sh` and `run_one.sh` and stubs
only the four expensive stages, behind the same argument/output contract. It has
caught seven real bugs so far (`--from-file` with no positionals, an unhelpful
unrecognized-input error, the double YouTube download, the
`$BASHPID`-in-substitution queue bug, a stage exiting 0 without writing its
artifacts, a `--clip` label that could not be parsed back on a resume, and a
resumed run failing its `frames` branch because the frames had been swept —
`mark_done` refused the missing manifest on a stage that had nothing to do).
Add to it when you touch orchestration.

The `--clip` split between the two shell suites is worth knowing: the pipeline
suite stubs ffmpeg and asserts on the argv and the DAG, and `test_media_e2e.sh`
runs the real thing against a real MP4 and probes the result with ffprobe. Only
the second could see that ffmpeg refuses to write a file named `.part`, and
only the first can afford to exercise nine different windows.

The Kaltura network seam is **not** in `test_media_e2e.sh`. `lib/test_kaltura.py`
drives a fake `requests.Session` instead, which asserts on what we send — the
`Referer`, the `widgetId`, the KS on the media URL — without needing a stub HTTP
server, and is the place to add assertions when the invocation changes.
`test_pipeline_e2e.sh` stubs only `kaltura.py`'s network half and delegates
`parse` to the real module, because the orchestration under test depends on what
the parser returns (the run id, and the canonical URL that replaces the blob).

`test_media_e2e.sh` is the counterpart: real media, real SDKs, stub servers
(`lib/fake_api_server.py`) speaking the providers' HTTP protocols. It is the
only place that can assert on **what we actually send**. For the summarizer
that seam is no longer HTTP — it is `lib/fake_claude_cli.py`, a stub
*executable* that `CLAUDE_CLI_BIN` points at. It records the argv, the piped
prompt and the auth vars that reached the child, so the test asserts that
`--effort` carries `SUMMARY_EFFORT`, that the frames arrive as JPEG image
blocks in a stream-json user message with `--tools ""` and no `--add-dir`,
that no filesystem path is in the prompt, that the copies sent are the
cropped ≤768px ones and the originals are untouched, and that
`ANTHROPIC_API_KEY` — deliberately exported by the test — did *not* reach the
CLI. The stub unpacks the stream-json line so its `prompt` field is still the
text the model read, and records the image blocks under `images`. It also drives `FAKE_CLAUDE_MODE=not-logged-in` to prove a signed-out CLI
fails as `BackendUnavailable` rather than being retried, and
`rate-limited-once` / `rate-limited` (with `FAKE_CLAUDE_RESET_IN` and
`CLAUDE_CLI_MAX_WAIT_SECONDS`) to drive the real wait-then-retry and the
pause through the real `summarize.py`, state.json included. The stub answers
in stream-json when asked to, with a `rate_limit_event` at a fixed 42% so
the ledger's numbers can be asserted exactly. When you change the
invocation, assert it here. Note the wait test really sleeps: the stub's
reset is 2s away and the margin is 60s, so that block takes about a minute.
`KEEP_TESTROOT=1` keeps the test root for inspection.

One pre-existing flaky failure on the author's desktop is worth knowing so
it is not mistaken for a regression: "language not sent" in the transcribe
section. (The earlier "no downscaled frame copies" failure went away with a
scratch venv built from `requirements.txt` — `/opt/meeting-bot-venv` on the
desktop is an empty 3.14 venv, so run the shell suites with
`MEETING_BOT_VENV=<a venv built with uv from requirements.txt>`.) The media
test's synthetic slides are flat colour, so the duplicate pass collapses all
three to one image block; that is the fixture, not a bug — real slides carry
text, which is what the texture hash keys on.

**What no test here covers:** Chrome actually joining a live Meet/Zoom call, a
real Kaltura tenant's access-control (`./verify_e2e.sh --kaltura` is the live
check, and needs no key), and
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
├── verify_e2e.sh                 <- live checks: preflight + mp4/YouTube/Kaltura/Meet/Zoom
├── trigger_server.py
├── meeting-bot-trigger.service   <- systemd unit (setup.sh --with-trigger)
├── meeting-bot-resume.service    <- `pipeline.sh --resume-all` for paused runs
├── meeting-bot-resume.timer      <-   ...every 15 min (setup.sh --with-resume-timer)
├── lib/
│   ├── runstate.py               <- run state + locking + CLI
│   ├── slotqueue.py              <- machine-wide component queue
│   ├── keyring.py                <- numbered API keys + rotation cursor
│   ├── paths.py / paths.sh       <- the five required output directories
│   ├── xsession.sh               <- per-run Xvfb display + PulseAudio sink
│   ├── resources.py              <- slides/notes from GitHub or a folder
│   ├── kaltura.py                <- Kaltura embeds: parse, media URL, captions
│   ├── clip.py                   <- --clip: window parsing + the ffmpeg cut
│   ├── combine.py                <- --combine: parts.json from the member runs, the run key
│   ├── run_one.sh                <- the per-run stage DAG
│   ├── fake_api_server.py        <- stub AssemblyAI/YouTube servers
│   ├── fake_claude_cli.py        <- stub `claude` binary for the media test
│   ├── test_runstate.py
│   ├── test_slotqueue.py
│   ├── test_keyring.py
│   ├── test_resources.py
│   ├── test_kaltura.py
│   ├── test_clip.py
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
        ├── summarize.md          <- default (see the load_prompt_template note)
        ├── summarize-v2.md       <- XML-tagged, worked example, cacheable prefix
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
