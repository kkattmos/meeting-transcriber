# CLAUDE.md — Project context for future Claude sessions

This file is the canonical reference for architectural decisions, conventions,
and non-obvious facts about this project. Future Claude sessions should read
this before exploring the source code so they don't have to re-derive context
that's already locked in. After every edit, make sure to update this so the
information is up-to-date. Always ask the user questions first.

User-facing docs (what this project does, how to run it) live in `README.md`.
This file is for the things that aren't obvious from reading code or git
history.

### Note from AssemblyAI
Before writing AssemblyAI code, read https://www.assemblyai.com/docs/agent-instructions.md
and https://www.assemblyai.com/docs/llms.txt. The API has changed — do not rely on
memorized parameter names.

## What this project is, in one paragraph

A meeting-bot for Proxmox VMs (Ubuntu 26.04 LTS Server minimal). Joins a
Google Meet or Zoom call in a persistent real-Chrome profile (so the Google
sign-in flow doesn't get blocked by automation-detection heuristics),
records both the screen and the meeting audio into an MP4, transcribes the
audio with the AssemblyAI pre-recorded API (or the youtube-transcript.io
API for YouTube URLs), and produces an AI summary that combines the
transcript with keyframes extracted from the screen recording. The three
stages are independent — each one has its own entry script and can be run without the
others.

## Configuration

All non-secret env vars live in a single `.env` file at the repo root.
`.env.example` is the committed template; users copy it to `.env`,
`chmod 600 .env`, and edit. The `.env` file is in `.gitignore` (alongside
`/opt/meeting-bot/secrets/`) so secrets never leak into commits.

Both `pipeline.sh` and `transcribe.sh` source `source_env.sh` from the
repo root, which loads `.env` into the calling shell. `summarize.py`
carries its own `_load_dotenv()` for the case where it's invoked directly
(not via `pipeline.sh`). The loader is "fill in unset values only" — an
already-exported env var (e.g. `KEY=val ./pipeline.sh ...`) always wins.

YouTube-API keys live in a separate JSON file because they're
inherently an array. Path: `/opt/meeting-bot/secrets/youtube_transcript_keys.json`,
overridable via `YT_TRANSCRIPT_KEYS_FILE` in `.env`.

## The three options at a glance

| Option | Entry | Output | Needs |
|---|---|---|---|
| 1. Screen record | `sudo -H ./screen/record_screen.sh <url> <name> [display_name]` | `recordings/<name>_<ts>.mp4` | Xvfb, Chrome profile, network |
| 2. Transcribe | `sudo -H ./transcribe/transcribe.sh <audio_or_video_or_yt> <name> [lang]` | `transcripts/<name>_<ts>.{txt,srt}` | ASSEMBLYAI_API_KEY (local files); youtube-transcript.io keys (YouTube URLs) |
| 3. AI summary | `python3 ./summarize/summarize.py <video> <transcript> [<out>]` | `summaries/<name>_<ts>.md` | ANTHROPIC_API_KEY, NVIDIA_NIM_API_KEY, or GOOGLE_API_KEY (FCC / NIM / Gemini / Ollama / fallback chain) |

The chain orchestrator is `./pipeline.sh` (runs all three in order). The
Tailscale trigger server (`trigger_server.py`) defaults to launching
`pipeline.sh`.

## Per-option reference

### Option 1 — Screen recording (`screen/record_screen.sh`)

- **What it captures**: video via `ffmpeg -f x11grab -video_size 1920x1080
  -framerate 15 -i :99`, audio via `ffmpeg -f pulse -i meeting_sink.monitor`,
  muxed to a single MP4 (`-c:v libx264 -preset ultrafast -crf 28 -c:a aac
  -b:a 128k -pix_fmt yuv420p`).
- **Display geometry is 1920x1080** across the board: the Xvfb head
  (`-screen 0 1920x1080x24` in both `first_time_login.sh` and
  `screen/record_screen.sh`), the Chrome window (launched with `--kiosk`
  + `--window-size=1920,1080` in both `first_time_login.sh` and
  `screen/capture.py`), and the ffmpeg `-video_size`. They all have to
  agree — a mismatch produces black edges. The number was chosen to
  match the default Proxmox console. `--kiosk` alone is not enough on
  some Xvfb/Chrome combos (Chrome can leave a few px of margin in
  --kiosk mode); `--window-size` pins the drawable area to the Xvfb
  head.
- **Join driver**: `screen/capture.py` (Playwright with `channel="chrome"`).
  Carries the Thai button labels (added previously when Google sign-in was
  being fixed). Same `ADMITTED_MARKER` + `KILL_SENTINEL` pattern as the
  original audio-only pipeline.
- **Path conventions**: `/opt/meeting-bot/{recordings,transcripts,summaries,frames}`.
- **Output files**: `recordings/<name>_<timestamp>.mp4` plus
  `recordings/<name>_<timestamp>_ffmpeg.log` (ffmpeg's stderr).
- **Failure modes**: if join fails, a `join_failed.png` or `not_admitted.png`
  lands in `recordings/`. If the MP4 is empty, check the `_ffmpeg.log`.

### Option 2 — Transcribe (`transcribe/transcribe.sh`)

- **Input**: any `.wav`, `.mp3`, `.mp4`, `.m4a`, `.webm`, `.ogg`, or a
  YouTube URL.
  - **Local files**: AssemblyAI's pre-recorded API
    (`transcribe/assemblyai_client.py`). MP3/MP4/M4A/WAV are sent
    directly; WEBM and OGG are first demuxed to MP3 with ffmpeg. The SDK
    handles the upload (`aai.Transcriber().transcribe(path, config=...)`)
    and emits a list of `{text, offset_ms, duration_ms}` segments — one
    per word. `transcribe.sh`'s shared writer turns that into the same
    `.txt` + `.srt` files whisper.cpp used to produce.
  - **YouTube URLs**: routed to the `youtube-transcript.io` API via
    `transcribe/yt_transcript_client.py`. Audio is NOT downloaded; the
    service returns YouTube's existing captions directly (orders of
    magnitude faster than AssemblyAI on a network-heavy call). See
    "YouTube support" below.
- **Language**: default `th` (Thai). Override with a 3rd CLI arg or via
  `ASSEMBLYAI_LANGUAGE` in `.env`. AssemblyAI uses ISO-639-1 codes:
  `th`, `en`, `auto`, etc. Ignored on the YouTube path because captions
  come pre-transcribed.
- **Model**: default is the AssemblyAI SDK's own fallback chain
  (`["universal-3-5-pro", "universal-2"]`). Override with
  `ASSEMBLYAI_MODEL=<name>` in `.env` to pin the leading entry;
  `universal-2` stays as the safety net.
- **No sudo required** for the actual API call — only needed if reading
  input files from /opt paths that root owns. The wrapper script is
  generally run with sudo to match the rest of the pipeline.
- **whisper.cpp is no longer wired into the pipeline** — its source is
  still cloned under `/opt/whisper.cpp` for backwards compatibility, but
  `transcribe.sh` does not call `whisper-cli`. If a future operator wants
  to bring it back, see the "Things future Claude MUST NOT change" list.

### Option 3 — AI summary (`summarize/summarize.py`)

- **Flow**: extract_frames.py -> frame manifest -> read transcript -> load
  prompt from `summarize/prompts/summarize.md` -> call LLM -> write Markdown.
- **Backend selection** via `SUMMARY_BACKEND` env var (`summarize/llm_client.py`
  owns the dispatch):
  - `anthropic` (default): uses the `anthropic` Python SDK. Honors
    `ANTHROPIC_BASE_URL` so the FCC free proxy works without code changes.
    Required: `ANTHROPIC_API_KEY`. Optional: `SUMMARY_MODEL` (default
    `claude-sonnet-4-5`).
  - `ollama`: hits `OLLAMA_HOST/api/generate` (default `http://localhost:11434`).
    Required: a vision-capable model like `llava:13b`. Set `OLLAMA_MODEL`.
  - `nvidia_nim`: hits `NVIDIA_NIM_BASE_URL/v1/chat/completions` (default
    `https://integrate.api.nvidia.com/v1`) via the `openai` Python SDK.
    Required: `NVIDIA_NIM_API_KEY`. Optional: `NVIDIA_NIM_MODEL` (default
    `meta/llama-3.1-70b-instruct`). Self-hosted NIM works too — just point
    `NVIDIA_NIM_BASE_URL` at it.
  - `gemini`: uses the `google-genai` SDK. Reads `GEMINI_MODEL` (default
    `gemini-2.5-flash`) and `GOOGLE_API_KEY` (also accepts `GEMINI_API_KEY`).
  - `fallback`: walks `SUMMARY_FALLBACK_CHAIN` in order. Each entry is one
    of `fcc`/`anthropic`/`nvidia_nim`/`gemini`/`ollama`. The first to
    succeed wins; the script logs each failure and advances. Default
    chain: `fcc,nvidia_nim,gemini`.
- **Auto-fallback rationale**: covers the case where the user's primary
  provider is rate-limiting, the FCC proxy is down, or a key was rotated
  and one variable wasn't refreshed. Cost is one extra retry per failed
  backend; benefit is fewer "stuck" pipelines. The chain is env-var driven
  so the order is per-run tunable.
- **FCC Claude config** (the user's stated preference):
  ```bash
  export ANTHROPIC_BASE_URL="https://<your-fcc-host>"
  export ANTHROPIC_API_KEY="<your-fcc-key>"
  export SUMMARY_MODEL="<the model FCC exposes>"
  # Or, to make the chain try FCC -> NIM -> Gemini automatically:
  export SUMMARY_BACKEND=fallback
  export SUMMARY_FALLBACK_CHAIN=fcc,nvidia_nim,gemini
  ```
- **NIM-only**: `export SUMMARY_BACKEND=nvidia_nim NVIDIA_NIM_API_KEY=...`.
- **Gemini-only**: `export SUMMARY_BACKEND=gemini GOOGLE_API_KEY=...`.
- **Frame extraction** (`screen/extract_frames.py`):
  - `SCENE_THRESHOLD` (default 0.3) — ffmpeg scene-change score cutoff.
  - `FRAME_PERIOD_SECONDS` (default 30) — periodic safety-net sample.
    Set to 0 to disable.
  - `FRAME_OUTPUT_DIR` (default `/opt/meeting-bot/frames`).
  - The output manifest is `frames/<name>/manifest.json` with a list of
    `{timestamp_s, kind, path}` entries. Kinds are `"scene_change"` or
    `"periodic"`.
  - Aggressive sampling: `FRAME_PERIOD_SECONDS=10 SCENE_THRESHOLD=0.2`.
  - Conservative (slides-only): `FRAME_PERIOD_SECONDS=300 SCENE_THRESHOLD=0.6`.
- **Why the scene-change + periodic combo**: scene-change catches slide
  transitions and shared-video cuts; periodic guarantees at least one
  frame every N seconds even on a static slide. The user explicitly asked
  about "user shares a video" — scene-change handles that natively (a
  shared video is just continuous changes), and the periodic pass is the
  belt-and-suspenders fallback. All four vision-aware backends (anthropic,
  ollama, nvidia_nim, gemini) receive the same frames, so behavior is
  consistent across providers.
- **Output**: `/opt/meeting-bot/summaries/<name>_<ts>.md`. Sections:
  Key decisions, Action items (with owners + due dates), Topics discussed
  (with timestamp citations), Slides / visuals referenced.

## Architectural conventions

- **All scripts must run with `sudo -H`**, matching how `setup.sh` was run.
  Without `-H`, Playwright looks for Chromium under a different home dir and
  fails with a confusing "Executable doesn't exist" error. This applies to
  the recording side (Chromium + Xvfb + PulseAudio all want the same env).
  Stage 3 (`summarize.py`) doesn't strictly need sudo but inherits it for
  consistency when run via `pipeline.sh`.
- **`/opt/meeting-bot` is the canonical output root.** Subdirs:
  `recordings/`, `transcripts/`, `summaries/`, `frames/`. Created by
  `setup.sh`. Anything the user-facing scripts write goes here.
- **`~/.meeting-bot/chrome-profile/` is the Chrome profile.** Written by
  `first_time_login.sh`, read by `screen/capture.py`. NEVER replace this
  path with anything that goes through Playwright's bundled Chromium
  (different binary, different cookies).
- **Xvfb is always on display :99 with a 1920x1080 head.** Both
  `first_time_login.sh` and `screen/record_screen.sh` start it. The
  pre-cleanup `pkill -f "Xvfb :99"` at the top of `record_screen.sh` is
  intentional — a leftover Xvfb from a previous crashed run will
  silently inherit state otherwise. The 1920x1080 head must match
  Chrome's `--kiosk` window AND ffmpeg's `-video_size`; if you change
  one, change all three.
- **The kill sentinel is `/tmp/meeting_bot_kill`.** Touched by the
  `Ctrl+\` SIGQUIT trap in `screen/record_screen.sh` and `pipeline.sh`, and
  by `kill_meeting.sh`. Polled by `screen/capture.py` in both wait loops.
  When it appears, the bot clicks "Leave" in Meet before exiting (so other
  participants see the bot leave, rather than vanishing).
- **The admission marker is `/tmp/meeting_bot_admitted`.** Touched by
  `screen/capture.py` when the bot is actually inside the call (not a
  lobby). Polled by `screen/record_screen.sh` before starting ffmpeg.
  Both files are cleared at the start of every run.

## Things future Claude MUST NOT change

These are decisions with a specific reason behind them. If a future Claude
session wants to change them, stop and confirm with the user first — they
represent deliberate trade-offs, not laziness.

- **Playwright uses `channel="chrome"`**, NOT the bundled Chromium. The
  bundled build gets Google's "This browser or app may not be secure" block
  on sign-in. The system-installed `google-chrome-stable` does not. Already
  fixed in `screen/capture.py`; do not revert to the bundled Chromium.
- **Login uses a direct `google-chrome` launch, not Playwright.** Even with
  `channel="chrome"`, Playwright injects automation flags (DevTools
  Protocol, `navigator.webdriver=true`) that Google detects. Only the direct
  shell launch in `first_time_login.sh` is clean enough. Do not "fix" this
  by routing the login through Playwright.
- **Chrome runs with `--no-sandbox`.** Required because `setup.sh` /
  `record_screen.sh` run as root (`sudo -H`). Sandbox + root = crash on
  launch. Already fixed in `screen/capture.py` and `first_time_login.sh`;
  do not remove.
- **Locale is `th-TH`**, so Thai participant names render in the chat.
  Side effect: Meet's UI labels come back in Thai. `screen/capture.py`
  carries both English and Thai labels for every selector to compensate.
  Do not drop the locale — it fixes a real visual bug.
- **The kill switch routes through the in-Meet Leave button**, not by
  killing the browser process. Other participants see the bot leave
  cleanly instead of vanishing mid-call. Do not "optimize" this by
  SIGKILL-ing the Python process.
- **H.264 encoder is `libx264 -preset ultrafast -crf 28`.** The ultrafast
  preset keeps CPU low on the 4-vCPU VM (no GPU). crf 28 is visually fine
  for talking heads + slides. Do not switch to a higher quality preset
  without considering CPU; do not switch to a different codec without
  checking ffmpeg is built with it.
- **The frame-sampling combo is scene-change + periodic.** Both passes
  emit frames; deduplication by ±half-period is in `extract_frames.py`.
  Do not replace with "scene-change only" — that misses the "user talks
  about a static slide for 5 minutes" case.
- **`--disable-features=ScreenCapture` is intentional.** It keeps the bot
  from accidentally sharing its screen. Layers 2 and 3 in
  `screen/capture.py` (dialog killer + "Stop presenting" monitor) are the
  catch-nets if Chrome renames the feature flag. Don't remove the flag
  just because it "looks restrictive" — the bot has no legitimate reason
  to share its screen.
- **Camera/mic mute is best-effort (log warning + continue), not abort.**
  A failed UI heuristic must not block real meetings. If the warning
  surfaces in production logs, fix the selectors — but the recording
  still proceeds.
- **Google Meet pre-join uses Tab-scan, not fixed Tab counts.** The
  pre-join DOM reorders frequently; identifying buttons by accessible
  name as we Tab through is the only durable approach. Don't "optimize"
  this into a fixed Tab-count + Enter sequence — it will silently break
  on the next Meet UI change. The Tab-scan runs alongside the existing
  post-admission `mute_av()` safety net, not in place of it.
- **YouTube URLs auto-route to youtube-transcript.io**, not to local
  Whisper and not to AssemblyAI. Whisper on CPU is too slow for YouTube
  (minutes per video on a 4-vCPU VM), and on some Thai videos it returns
  only placeholder text like `[เสีงพากย์ไทย]`. AssemblyAI is a paid
  transcription service; for YouTube we already have free captions via
  youtube-transcript.io. The third-party API returns real captions in
  seconds. Override per-run with `TRANSCRIBE_BACKEND=youtube_transcript`
  if you need to be explicit, but don't change the auto-route default.
- **whisper.cpp is no longer wired into the pipeline.** The
  `/opt/whisper.cpp` checkout is kept around for backwards compatibility
  (a few operators still inspect the binary) but `transcribe.sh` does not
  call `whisper-cli` on any path. Do not re-add a `TRANSCRIBE_BACKEND=whisper`
  escape hatch to `transcribe.sh` — if a future operator wants to use
  whisper.cpp, that's a separate decision and needs explicit sign-off.
  The `setup.sh` build of `/opt/whisper.cpp` can stay (idempotent,
  incremental rebuild is cheap) but its presence is decoupled from the
  pipeline.
- **Empty YouTube transcripts fail loudly** in `transcribe.sh`, NOT a
  silent fallback to local Whisper. Every API key hits the same
  upstream YouTube captions, so retrying won't help — we want the user
  to investigate (region, login state, captions availability) and re-run.
- **Multiple youtube-transcript.io accounts rotate round-robin** via the
  key file `/opt/meeting-bot/secrets/youtube_transcript_keys.json`. The
  cursor advances past each successful pick. Don't replace this with a
  single env var — the user explicitly asked for multi-account quota
  spreading.
- **The summarize fallback chain (`SUMMARY_BACKEND=fallback`) is the
  default mode going forward.** Single-backend mode still works for
  callers that want it, but the chain gives automatic failover across
  `fcc` / `nvidia_nim` / `gemini` and stops pipelines from getting
  stuck when one provider is rate-limited or down.
- **The YouTube download in `summarize.py` uses yt-dlp's single-stream
  format (`best[ext=mp4]/best`), NOT `bestvideo+bestaudio
  --merge-output-format mp4`.** The merge path now requires a JS runtime
  (deno) for YouTube extraction and a clean postprocess merge; the
  single-stream format returns one muxed file yt-dlp hands back
  without re-muxing. Don't "fix" this by adding `--merge-output-format`
  back — it'll reintroduce the JS-runtime dependency and the
  "Conversion failed" postprocess error. If a future YouTube change
  breaks `best[ext=mp4]/best`, add a runtime-agnostic format fall-back
  (e.g. add `best` to the chain) rather than re-enabling the merge.
- **The YouTube download tempdir is `/opt/meeting-bot/tmp/`, NOT
  `/tmp/`.** A previous run leaked a 1.7GB tempdir under `/tmp` (a
  small tmpfs on the VM) and starved the whole system. The tempdir is
  created on demand by `summarize.py`, cleaned up in a `finally` block
  loud-fail (no `ignore_errors`), and a startup sweep removes anything
  older than 24h. Don't move it back to `/tmp` — the `/opt` path is
  specifically chosen to avoid the small-tmpfs OOM scenario.

## YouTube support

Stages 2 (transcribe) and 3 (summarize) accept a YouTube URL in place of a
local file. The pipeline orchestrator (`pipeline.sh`) auto-routes YouTube
URLs to skip Stage 1 (recording) — there's no meeting to join.

- **URL forms**: `https://www.youtube.com/watch?v=<id>` and
  `https://youtu.be/<id>`. Playlists are not supported.
- **Transcribe (Stage 2) — API path** (the default for YouTube URLs):
  `transcribe/transcribe.sh` extracts the video ID and calls
  `transcribe/yt_transcript_client.py`, which hits
  `https://www.youtube-transcript.io/api/transcripts` over HTTP Basic auth
  using the `requests` library (urllib was rate-limited where `curl`
  worked — most plausibly a User-Agent / TLS-fingerprint difference;
  `requests` sends `python-requests/<v>` instead of urllib's
  `Python-urllib/<v>`. The client logs the outbound User-Agent to
  stderr on every call as a diagnostic). Audio is NOT downloaded;
  captions come back as `{text, offset_ms, duration_ms}` segments and
  are written to the same `.txt` + `.srt` files that Whisper would
  produce. This is orders of magnitude faster than local Whisper on CPU.
  Note: the API can return Thai re-voiced videos with only the
  placeholder text `[เสียงพากย์ไทย]` — that placeholder SURVIVES the
  normaliser (`_normalise_segments` keeps any non-empty text), so the
  script writes it through without erroring. The loud-fail path is
  reserved for genuinely empty responses (zero segments), not for
  placeholder-only text. Operators diagnose placeholder output by
  inspecting the `.txt` file.
- **Transcribe override**: `TRANSCRIBE_BACKEND=assemblyai` or
  `=youtube_transcript` selects the engine explicitly. The auto-route
  already dispatches by URL type (local files → AssemblyAI, YouTube URLs
  → youtube-transcript.io), so these overrides are only useful for tests
  or when you want to bypass the auto-route. whisper.cpp is no longer a
  backend — see the "Things future Claude MUST NOT change" list.
- **Multiple accounts**: populate
  `/opt/meeting-bot/secrets/youtube_transcript_keys.json` with one token
  per account:
  ```json
  {
    "keys": ["acct1-token", "acct2-token", "acct3-token"],
    "next_index": 0
  }
  ```
  The client round-robins through the list, advancing the cursor past
  each successful pick. The file is chmod 600 (the client enforces this
  on first write). If the file doesn't exist yet, the script creates a
  starter file with an empty `keys` list and exits — populate it and
  re-run.
- **Retry behaviour**: a single transcript fetch tries each configured
  key in turn. Retryable failures (HTTP 401 / 403 / 429 / 5xx, network
  error) trigger an advance to the next key. After all keys are tried,
  the last error is raised. An empty response (no segments, or
  placeholder-only) is treated as "no captions exist for this video" —
  every key hits the same upstream YouTube captions, so retrying won't
  help. The script fails loudly with exit code 2.
- **Summarize (Stage 3)**: `summarize/summarize.py` downloads a single
  muxed file via `yt-dlp -f "best[ext=mp4]/best"` (no `--merge-output-format`,
  no separate video+audio streams). The previous bestvideo+bestaudio + merge
  path now requires a JS runtime for YouTube extraction and the merge step
  postprocesses; the single-stream format sidesteps both. If the chosen
  video only exposes webm, yt-dlp falls back to that — ffmpeg/extract_frames
  handle either container. The download lands in a per-run tempdir under
  `/opt/meeting-bot/tmp/meeting-bot-yt-XXX/` (created on demand) and is
  removed in a `finally` at the end; cleanup errors are logged loudly
  (no silent `ignore_errors`) so a future leak is visible. A startup
  sweep removes any `meeting-bot-yt-*` tempdir older than 24h to self-heal
  cases where the process was killed before the finally ran.
- **Pipeline routing** (`pipeline.sh`):
  - `meet.google.com` or `zoom.us` → all 3 stages.
  - `youtube.com` / `youtu.be` → skip Stage 1, run 2 + 3 against the URL.
  - Anything else → error and exit.
- **Filename convention for YouTube**: when `pipeline.sh` auto-routes, the
  transcript and summary basenames are derived from the video ID
  (`yt_<video_id>_<timestamp>.{txt,md}`). The user's meeting name argument
  is overridden in this case so the two stages share a consistent name.
- **Standalone invocations** still work — the user can run
  `transcribe/transcribe.sh` or `summarize/summarize.py` with a YouTube URL
  directly.
- **yt-dlp is still installed** by `setup.sh` — required for
  `summarize.py`'s YouTube video download. Don't fall back to the apt
  package; YouTube breaks yt-dlp regularly and a stale binary is the #1
  cause of silent failures.

## Camera/mic/screen-share guards

The bot joins meetings with the camera and microphone **on by default** (Meet
sees any browser-with-media-permission as a real participant). Three layers
keep this from leaking to other participants and stop accidental screen-share.

### Mute camera + mic before recording starts

**Pre-join (Google Meet only):** `screen/capture.py`'s
`prejoin_mute_and_join_google_meet()` runs immediately after `page.goto()`.
It fills the display-name field, then Tab-walks the pre-join screen
identifying buttons by accessible name (English + Thai) and pressing Enter
to click the camera-off, mic-off, and Join buttons. Right before that,
`go_fullscreen()` presses F11 + calls the Fullscreen API so the Chrome
window fills the Xvfb display during the join flow. The Zoom path is
unchanged.

**Post-admission safety net:** `screen/capture.py`'s
`mute_av(page, platform)` is called once, after admission and before
`ADMITTED_MARKER` is touched. It stays in place even though the pre-join
flow now mutes first — the safety net covers the case where the pre-join
heuristic missed (e.g. lobby screen, or a future UI reorder). Strategy:

1. Try keyboard shortcuts (Meet: Ctrl+E camera, Ctrl+D mic; Zoom: Alt+V,
   Alt+A). User explicitly asked for this as a fast path.
2. Fall back to clicking aria-label buttons: "Turn off camera" /
   "Mute microphone" / "หยุดนำเสนอ" (already-off = "Turn on camera" /
   "Unmute", which is treated as success).

Failure mode is **log warning + continue** — failing the recording over a UI
heuristic would block real meetings. If the warning shows up, fix the
selectors; the screen capture is the real product.

### Three-layer screen-share defense

All three layers run on every poll inside `wait_until_meeting_ends`:

- **Layer 1: Chrome flag.** `--disable-features=ScreenCapture` is passed to
  `launch_persistent_context`. Disables the `getDisplayMedia` API entirely.
  If Chrome ever renames the feature flag, Layers 2 and 3 are the catch-nets.
- **Layer 2: Native dialog killer.** `block_screen_share_dialog(page)` scans
  page text for "see your screen" / "share your screen" and clicks any button
  whose label contains "Block" / "Deny" / "Cancel".
- **Layer 3: In-Meet banner monitor.** `stop_unwanted_presenting(page)` looks
  for "You are presenting" / "Stop presenting" / "หยุดนำเสนอ" in page text and
  clicks Stop presenting, logging a loud warning. **Click + log warning** —
  recording continues.

## Auto-exit policy

The bot leaves a meeting under any of these conditions (first one wins):

1. **Kill sentinel** (`/tmp/meeting_bot_kill`) — set by `Ctrl+\` in the
   recording terminal or by `kill_meeting.sh`. Clean Leave click.
2. **Page closed / title changed** — "meeting has ended", "call ended",
   "left the meeting" in `page.title()`.
3. **Mass exodus** — participant count falls below
   `int(peak_count * DROP_RATIO_THRESHOLD)` (default 30%) for
   `LOW_COUNT_CONFIRMATIONS` (default 2) consecutive polls. Tunable in
   `screen/capture.py`.
4. **Hard timeout** — wall-clock time in the meeting exceeds
   `MAX_MEETING_SECONDS` (default `4 * 3600`, env-overridable as
   `MAX_MEETING_MINUTES`). This is a backstop so the bot never gets stuck.
5. **Idle auto-leave** — participant count stays at 1 (only the bot)
   or 2 (only you + the bot) for `IDLE_LEAVE_SECONDS` (default `5 * 60`,
   env-overridable as `IDLE_LEAVE_MINUTES`, set to 0 to disable). Catches
   the "test call with just me" case the mass-exit rule misses (peak is
   2, so 30% of peak is 0 — never triggers). Independent of mass-exit;
   both can fire on the same call. Counts that can't be parsed (scrape
   miss) leave the timer untouched rather than resetting it.

Configuration examples:

```
# In screen/capture.py (top of file):
DROP_RATIO_THRESHOLD = 0.25      # leave earlier (was 0.30)
LOW_COUNT_CONFIRMATIONS = 1      # react on first drop, not second

# Override the hard timeout for a single run:
MAX_MEETING_MINUTES=120 sudo -H ./screen/record_screen.sh ...   # 2h cap

# Auto-leave when only the bot (count=1) or only the bot + one other
# person (count=2) for N minutes:
IDLE_LEAVE_MINUTES=1 sudo -H ./screen/record_screen.sh ...       # 1m for tests
```

## setup.sh re-run behavior

`setup.sh` is now **idempotent** for re-runs:

- `/opt/whisper.cpp` already exists? Run `git pull` instead of cloning.
  Always run `cmake --build build` after — incremental builds are cheap and
  guarantee the binary is in sync with the source.
- `yt-dlp` is re-downloaded every time (a few hundred KB, no cost). The
  `--version` sanity check after download fails loudly if the binary is
  broken (e.g. a firewall returning HTML instead of a binary).
- `deno` is installed only when missing (`command -v deno` check). The
  latest GitHub release zip is downloaded to `/usr/local/bin/deno`,
  chmod a+rx, sanity-checked with `deno --version`. Required by yt-dlp
  for YouTube extraction on newer formats.

If `/opt/whisper.cpp` got into a broken state (failed mid-build, etc.):

```
sudo rm -rf /opt/whisper.cpp && sudo -H ./setup.sh
```

The other apt packages, Python venv, Chrome, and `mkdir -p` directories are
all idempotent already (apt skips installed packages; mkdir -p is a no-op).

## Things future Claude might want to add

Out of scope right now, but easy to bolt on:

- Auto-upload summaries to Slack / Notion / Obsidian after pipeline.sh.
- Real-time incremental summary during the meeting (would require a long-
  running agent process; current pipeline is post-meeting only).
- Whisper speaker diarization (would let the summary attribute quotes
  without the model having to infer from context).
- A web UI for picking which past meeting to re-summarize with different
  LLM settings.

## File layout (post-split)

```
.
├── README.md
├── CLAUDE.md                                  <- this file
├── .gitignore                                 <- excludes .env + /opt/meeting-bot/secrets/
├── .env.example                               <- template for the per-user .env
├── source_env.sh                              <- loader for the .env file
├── setup.sh
├── audio-setup.sh
├── first_time_login.sh
├── kill_meeting.sh
├── trigger_server.py
├── meeting-bot-trigger.service
├── pipeline.sh                                <- chains the three options
├── screen/
│   ├── record_screen.sh                       <- Option 1 entry
│   ├── capture.py                             <- Playwright join driver
│   └── extract_frames.py                      <- frame extractor for Option 3
├── transcribe/
│   ├── transcribe.sh                          <- Option 2 entry
│   └── yt_transcript_client.py                <- youtube-transcript.io client (round-robin)
└── summarize/
    ├── summarize.py                           <- Option 3 entry
    ├── llm_client.py                          <- Anthropic / FCC / NIM / Gemini / Ollama + fallback chain
    └── prompts/
        └── summarize.md                       <- the prompt template
```

The legacy files `record_and_transcribe.sh` and `join_meeting.py` were the
audio-only predecessors; their logic has been folded into `screen/record_screen.sh`
and `screen/capture.py`. They are not kept as shims.
