# Meeting recording + transcription + AI summary bot

Joins a Zoom or Google Meet call using your logged-in account, records the
**screen + audio** to MP4, transcribes the audio locally with whisper.cpp,
and (optionally) produces an AI summary that combines the transcript with
key frames extracted from the recording.

The project is split into three composable options:

| | What it does | Entry script | Output |
|---|---|---|---|
| **Option 1: Screen record** | Joins the meeting and records the screen + meeting audio to MP4 | `./screen/record_screen.sh` | `/opt/meeting-bot/recordings/<name>_<ts>.mp4` |
| **Option 2: Transcribe** | Voice-to-text on any audio/video file via whisper.cpp; YouTube URLs route to the youtube-transcript.io API (multi-account round-robin) | `./transcribe/transcribe.sh` | `/opt/meeting-bot/transcripts/<name>_<ts>.{txt,srt}` |
| **Option 3: AI summary** | Sends transcript + extracted keyframes to a vision-capable LLM (FCC Claude, NVIDIA NIM, Google Gemini, or Ollama; auto-fallback chain supported) | `./summarize/summarize.py` | `/opt/meeting-bot/summaries/<name>_<ts>.md` |

Run them individually, or use the chain:
```bash
sudo -H ./pipeline.sh "https://meet.google.com/abc-defg-hij" "Weekly Standup"
# runs Option 1 -> Option 2 -> Option 3 in sequence.
```

`CLAUDE.md` is the per-option reference for future Claude sessions — it
holds usage, env-var configuration, debugging tips, and architectural
context that doesn't belong in the user-facing README.

Target OS: **Ubuntu 26.04 LTS Server, minimized cloud image**.

## VM sizing (Proxmox)

| | Minimum (works, tight) | Recommended |
|---|---|---|
| vCPU | 2 | 4 |
| RAM | 4 GB | 8 GB |
| Disk | 20 GB | 40–50 GB |

Why: the OS itself needs very little (Ubuntu Server minimal runs in ~1.5GB
RAM / 4GB disk), but Chromium + Xvfb + PipeWire/PulseAudio + ffmpeg running
concurrently during a call want headroom, and whisper.cpp's `small` model
needs roughly 1–1.5GB RAM while transcribing (more for `medium`). The
screen recorder adds an x11grab stream and H.264 encode on top of the
existing audio path — the recommended config already covers this comfortably.
Disk adds up over time: MP4 recordings are ~200-400MB per hour of meeting
(H.264 ultrafast preset, crf 28) and aren't auto-deleted, so budget disk
for how many hours of meetings you'll keep before manually clearing
`/opt/meeting-bot/recordings`.

## Thai language support

- `setup.sh` installs `fonts-thai-tlwg` (so Thai names/chat render correctly
  in the browser) and generates the `th_TH.UTF-8` locale.
- It downloads whisper.cpp's **multilingual** `small` model (not the `.en`
  English-only variant) - required for Thai transcription of local
  recordings to work at all.
- For **YouTube URLs**, the youtube-transcript.io API returns YouTube's
  existing Thai captions directly, so no model is needed. This is the
  recommended path for Thai YouTube content — it both avoids the slow
  local Whisper pass and sidesteps the failure mode where Whisper on a
  re-voiced Thai video returns only placeholder text like `[เสียงพากย์ไทย]`.
- For **local recordings** (`.wav`/`.mp4`/`.m4a`/`.mkv`),
  `transcribe/transcribe.sh` defaults to `-l th` for transcription. Override
  per-call with a 3rd argument, e.g.
  `transcribe/transcribe.sh <audio> <name> auto` to auto-detect language,
  or `en` for English-only meetings.
- If Thai accuracy with `small` isn't good enough, download the `medium`
  model (`bash ./models/download-ggml-model.sh medium` inside
  `/opt/whisper.cpp`) and point `WHISPER_MODEL` in `transcribe/transcribe.sh`
  at `ggml-medium.bin`. Expect roughly 3x the RAM and transcription time.

## Architecture

1. You paste a meeting link into `pipeline.sh` (or one of the standalone options)
2. A headless-but-rendered Chromium browser (Playwright) joins the call using
   a persistent, already-logged-in profile
3. Xvfb provides a virtual display for the browser (no physical monitor needed)
4. If a waiting room applies, the bot waits for host admission before
   recording starts (up to `ADMIT_TIMEOUT_SECONDS`)
5. ffmpeg captures:
   - **Video** from `DISPLAY=:99` via `x11grab` (1280x800, 15 fps)
   - **Audio** from `meeting_sink.monitor` (the same virtual PulseAudio sink
     the bot has always used)
   - Both streams are muxed into a single MP4 (H.264 + AAC).
6. Recording stops when the browser detects the call ended, or when the
   participant-count heuristic decides most people have left and leaves
   on its own (see "Auto-leave on mass exodus" below)
7. whisper.cpp transcribes the MP4's audio track into `.txt` and `.srt`
   (only when option 2 or the chain is run)
8. The AI agent extracts keyframes from the MP4 + reads the transcript +
   asks the configured LLM for a structured Markdown summary
   (only when option 3 or the chain is run)

## One-time setup

**Run every script in this project with `sudo -H`, consistently.**
`setup.sh` installs the venv, Chromium, and whisper.cpp under `/root` (because
it's run with sudo). If a later script runs as a different user, or as root
without `-H`, Playwright looks for Chromium under a different, empty home
directory and fails with a confusing "Executable doesn't exist" error.

```bash
chmod +x setup.sh audio-setup.sh first_time_login.sh \
         screen/record_screen.sh transcribe/transcribe.sh pipeline.sh kill_meeting.sh
sudo -H ./setup.sh
```

Then log into your Google and Zoom accounts **once**, inside the same browser
profile the bot will reuse later:

```bash
sudo -H ./first_time_login.sh
```

This opens a VNC session (`http://<vm-ip>:6080/vnc.html`) with a real browser
window. Log into Google (accounts.google.com) and Zoom (zoom.us) there, then
press Ctrl+C in the terminal to close it. The session/cookies persist in
`~/.meeting-bot/chrome-profile`, so this normally only needs to be repeated
when a session expires or a login triggers extra verification.

## Day-to-day usage

### All three options at once (the common case)

```bash
sudo -H ./pipeline.sh "https://meet.google.com/abc-defg-hij" "Weekly Standup"
# or
sudo -H ./pipeline.sh "https://zoom.us/j/1234567890" "Client Call"
```

Outputs land in:
- `/opt/meeting-bot/recordings/<name>_<timestamp>.mp4`
- `/opt/meeting-bot/transcripts/<name>_<timestamp>.txt` and `.srt`
- `/opt/meeting-bot/summaries/<name>_<timestamp>.md`

### Just one option

```bash
# Option 1 - record only (run again later to transcribe/summarize)
sudo -H ./screen/record_screen.sh "https://meet.google.com/abc-defg-hij" "Weekly Standup"

# Option 2 - transcribe an existing file (any WAV, MP4, M4A, MKV)
sudo -H ./transcribe/transcribe.sh /opt/meeting-bot/recordings/Weekly_Standup_<ts>.mp4 "Weekly Standup"

# Option 3 - summarize from a transcript + video
python3 ./summarize/summarize.py \
  /opt/meeting-bot/recordings/Weekly_Standup_<ts>.mp4 \
  /opt/meeting-bot/transcripts/Weekly_Standup_<ts>.txt
```

### YouTube URLs

Stages 2 and 3 also accept a YouTube URL. Stage 2 (transcribe) uses the
**youtube-transcript.io** API — fast and returns YouTube's existing captions
directly; no audio download, no Whisper on CPU. Stage 3 (summarize) still
uses `yt-dlp` to download the video for frame extraction.

```bash
# Transcribe a YouTube video (requires at least one API key configured)
sudo -H ./transcribe/transcribe.sh "https://www.youtube.com/watch?v=xyz" "talk"

# Summarize a YouTube video (transcript + video frames)
python3 ./summarize/summarize.py \
  "https://www.youtube.com/watch?v=xyz" \
  /opt/meeting-bot/transcripts/<existing_transcript>.txt

# Full pipeline for a YouTube URL - skips recording, runs 2 + 3 only
sudo -H ./pipeline.sh "https://www.youtube.com/watch?v=xyz" "talk"
```

The transcript and summary filenames are derived from the video ID, e.g.
`/opt/meeting-bot/transcripts/yt_xyz_<ts>.txt`.

#### YouTube API keys (one-time, per account)

The youtube-transcript.io API uses HTTP Basic auth with a single token per
account. To spread quota across multiple accounts, the transcriber
round-robins through a key list:

```bash
sudo nano /opt/meeting-bot/secrets/youtube_transcript_keys.json
# {
#   "keys": ["acct1-token", "acct2-token", "acct3-token"],
#   "next_index": 0
# }
```

`setup.sh` creates a starter file with an empty `keys` list on first run;
just add your tokens and re-run. The cursor at `next_index` advances past
each successful pick, so a sequence of runs deterministically walks the
list. The file is `chmod 600` (the client enforces this on first write).

The keys file path defaults to
`/opt/meeting-bot/secrets/youtube_transcript_keys.json` and can be
overridden by setting `YT_TRANSCRIPT_KEYS_FILE=` in your `.env`.

If the API returns no usable transcript (no captions exist, or captions
are placeholder strings like `[เสียงพากย์ไทย]`), the transcriber fails
loudly rather than silently falling back to local Whisper. Set
`TRANSCRIBE_BACKEND=whisper` to force the old yt-dlp + Whisper path on a
YouTube URL.

## Leaving a meeting early (kill switch)

Sometimes you need the bot to leave right now without you being at the
terminal — a meeting gets cut short, you started it from your phone via
`trigger_server.py`, or you just want to wrap up.

Two ways to do it:

1. **From the recording terminal: press `Ctrl+\`** (SIGQUIT). The shell's
   signal trap touches a kill sentinel; the Python join script notices on
   its next poll (≤15s), clicks the in-Meet "Leave" button so participants
   see the bot go, and exits. ffmpeg finalizes the MP4, and the chain (if
   running) proceeds to the transcribe + summarize stages.

2. **From any other terminal: `sudo ./kill_meeting.sh`**. Same outcome —
   touches the sentinel, sends SIGTERM to the running orchestrator + join
   driver, waits up to 10s for clean exit, then escalates only on the Python
   process if needed. Use this when the recording was launched in the
   background (e.g. via `trigger_server.py`, which detaches from any terminal).

Both paths deliberately route through the in-Meet Leave button rather
than yanking the browser out from under Meet, so other participants see
the bot leave cleanly instead of vanishing mid-call.

## Waiting rooms / host approval

`screen/capture.py` explicitly detects a waiting-room/lobby state ("waiting
for the host", "ask to join", etc) separately from being admitted (presence
of a "Leave call"/"Leave meeting" button). It polls every 5s for up to 10
minutes (`ADMIT_TIMEOUT_SECONDS`) before giving up and exiting — no recording
happens until admission is confirmed, so you won't get a file full of lobby
silence.

## Auto-leave on mass exodus

The script reads the participant count Zoom/Meet display in their own UI and
tracks its peak. It auto-leaves if, for two consecutive checks ~15s apart
(`POLL_SECONDS` / `LOW_COUNT_CONFIRMATIONS`):
- the count drops to 1 (everyone else left), or
- the count falls below 30% of its peak (`DROP_RATIO_THRESHOLD`) — catches
  the "call technically still open but everyone bailed" case

Tune these constants at the top of `screen/capture.py` if it's leaving too
eagerly or not eagerly enough for your meeting sizes.

## Remote trigger (start a recording from your phone)

`trigger_server.py` exposes a small authenticated HTTP endpoint that starts
`pipeline.sh` in the background. Meant to be reachable only over your
Tailscale network — no need to expose it publicly.

Setup:
```bash
sudo cp meeting-bot-trigger.service /etc/systemd/system/
sudo cp trigger_server.py /opt/meeting-bot/trigger_server.py
echo "MEETING_BOT_TOKEN=$(openssl rand -hex 24)" | sudo tee /etc/meeting-bot.env
sudo systemctl daemon-reload
sudo systemctl enable --now meeting-bot-trigger
```

Note the generated token in `/etc/meeting-bot.env` — you'll need it below.

Restrict it to Tailscale only (adjust interface name if different):
```bash
sudo ufw allow in on tailscale0 to any port 8765 proto tcp
sudo ufw deny 8765
```

Trigger from your phone (e.g. via an SSH/Shortcuts app that can run curl, or
Termux on Android):
```bash
curl -X POST http://<tailscale-hostname>:8765/trigger \
  -H "Authorization: Bearer <token from /etc/meeting-bot.env>" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://meet.google.com/abc-defg-hij", "name": "Client Call"}'
```
Returns `202` immediately; the full pipeline (record + transcribe + summarize)
runs in the background on the VM.

## Configuration via `.env`

All env vars live in a single file at the repo root. Copy the template,
edit, done:

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

`pipeline.sh`, `transcribe.sh`, and `summarize.py` all load `.env`
automatically (`summarize.py` has a tiny Python loader for direct
invocations). One-off overrides via `KEY=val ./pipeline.sh ...` still
win — `.env` only fills in values that aren't already exported.

The full list of supported keys is in `.env.example` with comments
describing each. The most common ones:

| Key | What it does |
|---|---|
| `SUMMARY_BACKEND` | `anthropic`, `nvidia_nim`, `gemini`, `ollama`, or `fallback` |
| `SUMMARY_FALLBACK_CHAIN` | Comma-separated order for fallback mode. Default `fcc,nvidia_nim,gemini` |
| `ANTHROPIC_API_KEY` | FCC Claude |
| `NVIDIA_NIM_API_KEY` | NVIDIA NIM |
| `GOOGLE_API_KEY` (or `GEMINI_API_KEY`) | Google Gemini |
| `WHISPER_LANGUAGE` | Whisper default for local files (default `th`) |
| `SCENE_THRESHOLD` / `FRAME_PERIOD_SECONDS` | Frame-extraction tuning |
| `MAX_MEETING_MINUTES` | Hard cap on meeting recording length |

YouTube-API keys live in a separate JSON file because the format is
inherently array-shaped:

```bash
sudo nano /opt/meeting-bot/secrets/youtube_transcript_keys.json
```

See "YouTube API keys" under "YouTube URLs" below.

## AI summary backends

The summary step (`summarize/summarize.py`) sends the transcript + extracted
keyframes to a vision-capable LLM. Four backends are supported, plus an
auto-fallback chain that walks them in order until one succeeds.

| Backend | `SUMMARY_BACKEND` | Required env vars | Notes |
|---|---|---|---|
| FCC Claude (default) | `anthropic` | `ANTHROPIC_API_KEY`, optional `ANTHROPIC_BASE_URL` + `SUMMARY_MODEL` | Honors the FCC proxy URL the same way the `anthropic` SDK always has. |
| NVIDIA NIM | `nvidia_nim` | `NVIDIA_NIM_API_KEY`, optional `NVIDIA_NIM_BASE_URL` (default `https://integrate.api.nvidia.com/v1`) + `NVIDIA_NIM_MODEL` (default `meta/llama-3.1-70b-instruct`) | Speaks the OpenAI chat-completions protocol; self-hosted NIM works too. |
| Google Gemini | `gemini` | `GOOGLE_API_KEY` (or `GEMINI_API_KEY`), optional `GEMINI_MODEL` (default `gemini-2.5-flash`) | Uses the `google-genai` SDK. |
| Ollama (local) | `ollama` | `OLLAMA_HOST`, `OLLAMA_MODEL` (default `llava:13b`) | Original local-only path. |
| Auto-fallback | `fallback` | the union of each backend you wire in | Walks `SUMMARY_FALLBACK_CHAIN` in order; first to succeed wins. Default chain: `fcc,nvidia_nim,gemini`. |

Quick examples:

```bash
# FCC Claude only (original behavior)
export SUMMARY_BACKEND=anthropic
export ANTHROPIC_BASE_URL="https://<your-fcc-host>"
export ANTHROPIC_API_KEY="<your-fcc-key>"
python3 ./summarize/summarize.py recording.mp4 transcript.txt

# NVIDIA NIM only
export SUMMARY_BACKEND=nvidia_nim
export NVIDIA_NIM_API_KEY="nvapi-..."
python3 ./summarize/summarize.py recording.mp4 transcript.txt

# Auto-fallback: try FCC, then NIM, then Gemini
export SUMMARY_BACKEND=fallback
export SUMMARY_FALLBACK_CHAIN=fcc,nvidia_nim,gemini
export ANTHROPIC_API_KEY=... NVIDIA_NIM_API_KEY=... GOOGLE_API_KEY=...
python3 ./summarize/summarize.py recording.mp4 transcript.txt
```

All four vision-aware backends receive the same frames + the same
prompt template, so behavior is consistent across providers. The fallback
chain costs one extra retry per failed backend — its purpose is to keep
the pipeline running when one provider is rate-limiting or down.

## Known fragility / things to watch

- **Zoom and Google Meet change their web UI periodically.** `screen/capture.py`
  looks for buttons/text by visible label ("Join now", "Ask to join", "Leave
  call", waiting-room phrases, etc). If a join or admission check fails,
  screenshots land in `/opt/meeting-bot/recordings/` (`join_failed.png`,
  `not_admitted.png`) — check those first, then update the selectors.
- **Auto-leave is a heuristic, not a guarantee.** The participant count is
  scraped with regex over visible page text (see `get_participant_count()`),
  since neither platform exposes it as a stable API in the web client. If
  that text format changes, or a meeting's UI never shows a count, the count
  reads as `None` and the bot falls back entirely on "page closed" / "meeting
  ended" title detection to know when to stop.
- **Admission gating relies on a marker file** (`/tmp/meeting_bot_admitted`).
  `screen/capture.py` touches it once it detects it's actually inside the call
  (not a waiting room); `screen/record_screen.sh` polls for it before
  starting `ffmpeg`, so recording only covers time you were actually admitted.
  If `capture.py` crashes between "admitted" and "meeting ends" without
  cleaning up, a stale marker could in theory affect the *next* run - this
  is unlikely (marker is also cleared at the start of every run) but worth
  knowing if recordings ever start unexpectedly early.
- **No native recording indicator.** This pipeline captures audio via a
  virtual PulseAudio sink, entirely separate from Zoom's/Meet's own "Record"
  feature — it never clicks their record button, so none of their on-screen
  recording banners appear to other participants. Worth being deliberate
  about whether that's acceptable for your meetings; see the recording-
  transparency discussion earlier in this conversation if you want the bot
  to trigger native recording or announce itself instead.
- **Bot-detection risk**: logging into Google/Zoom via an automated browser
  can occasionally trigger suspicious-activity checks. Logging in once,
  manually, via `first_time_login.sh`'s VNC session (rather than scripting
  the login itself) avoids most of this.
- **CPU load**: with 4 vCPUs and no GPU, whisper.cpp's `small` multilingual
  model (the current default, needed for Thai) runs comfortably faster than
  real-time. `medium` is ~3x slower/heavier but meaningfully more accurate,
  especially for Thai - see the Thai language section above for how to switch.
  The H.264 encoder in the screen recorder uses `-preset ultrafast` and
  `-crf 28`, which trades a little quality for much lower CPU cost; this
  is fine for talking-heads + slides.
- **`trigger_server.py` runs as root** (see `meeting-bot-trigger.service`) so
  it can launch the recording pipeline without a login session. It's gated
  by a bearer token and meant to be reachable only over Tailscale - don't
  expose port 8765 publicly.
- **Camera/mic are muted before recording starts** via `mute_av()` in
  `screen/capture.py` (keyboard shortcuts first, then aria-label fallbacks).
  If the heuristic fails, the recording still proceeds with a warning logged
  — check the warning and update the selectors if it ever shows up.
- **Screen-share is blocked three ways** in `screen/capture.py`: a Chrome
  flag (`--disable-features=ScreenCapture`), a runtime dialog killer, and
  a "Stop presenting" banner monitor that auto-clicks if the bot somehow
  starts presenting. The recording continues in all cases; the monitor
  just logs a loud warning when it kicks in.
- **A hard max-duration timeout** (default 4 hours, override with
  `MAX_MEETING_MINUTES=N`) is the backstop for the mass-exit heuristic —
  the bot will always leave the meeting after N minutes, no matter what
  participant-count telemetry says.

## Files

| File | Purpose |
|---|---|
| `setup.sh` | One-time install of all dependencies (idempotent: skips existing whisper.cpp via `git pull`; installs `yt-dlp` from GitHub releases) |
| `.env.example` | Template for the per-user `.env` config (copy to `.env` and edit) |
| `source_env.sh` | Tiny `.env` loader sourced by every entry script |
| `audio-setup.sh` | Creates the PulseAudio virtual sink |
| `first_time_login.sh` | One-time interactive Google/Zoom login via VNC |
| `screen/record_screen.sh` | Option 1 entry — Xvfb + Playwright + ffmpeg x11grab -> MP4 |
| `screen/capture.py` | Playwright driver that joins the call (carries Thai labels + kill sentinel) |
| `screen/extract_frames.py` | Scene-change + periodic frame extractor for the AI summary |
| `transcribe/transcribe.sh` | Option 2 entry — accepts WAV / MP4 / M4A / MKV; YouTube URLs auto-route to the youtube-transcript.io API |
| `transcribe/yt_transcript_client.py` | YouTube captions API client with multi-account round-robin key file |
| `summarize/summarize.py` | Option 3 entry — frames + transcript -> Markdown summary |
| `summarize/llm_client.py` | Pluggable LLM client (Anthropic / FCC / NVIDIA NIM / Gemini / Ollama) + auto-fallback chain |
| `summarize/prompts/summarize.md` | The summary prompt template |
| `pipeline.sh` | Chains Option 1 -> 2 -> 3 in sequence |
| `kill_meeting.sh` | Standalone "leave now" (also: Ctrl+\ in the recording terminal) |
| `trigger_server.py` | Tailscale-only HTTP endpoint that launches `pipeline.sh` |
| `meeting-bot-trigger.service` | systemd unit for `trigger_server.py` |
| `CLAUDE.md` | Per-option reference for future Claude sessions (env vars, debugging, conventions) |