# RUNBOOK

Copy-pasteable commands for every component, verified against the actual
source in this checkout (not the README, which lags in places — see
`ARCHITECTURE.md`). Run from the repo root unless noted.

All commands assume `setup.sh` has already been run once and `.env` exists
(`cp .env.example .env` then fill in keys).

---

## 1. One-time setup

```bash
sudo -H ./setup.sh
```
Installs system packages, the Python venv at `/opt/meeting-bot-venv`, Playwright
+ Chromium, and Thai locale/fonts. Target: Ubuntu 26.04 LTS, 4 vCPU / 8GB.

```bash
sudo -H ./first_time_login.sh
```
Run once after setup, and again whenever your Google/Zoom session expires.
Opens a viewable browser via noVNC (port 5901) so you can log in interactively
using the same persistent Chromium profile the bot reuses later.

---

## 2. Full pipeline (record → transcribe → summarize)

```bash
sudo -H ./pipeline.sh "<meeting_or_youtube_url_or_local_file>" "Meeting Name" ["Display Name"] [language]
```

Examples — one per input type in the diagram:

```bash
# Google Meet
sudo -H ./pipeline.sh "https://meet.google.com/abc-defg-hij" "Weekly Standup" "Meeting Bot" th

# Zoom
sudo -H ./pipeline.sh "https://zoom.us/j/1234567890" "Client Call" "Meeting Bot" th

# YouTube link (no sudo actually needed here — no browser/display involved —
# but sudo -H is harmless and keeps the command uniform)
sudo -H ./pipeline.sh "https://www.youtube.com/watch?v=dQw4w9WgXcQ" "" "" en

# Direct local file already on disk
sudo -H ./pipeline.sh "/home/you/recordings/existing_meeting.mp4" "" "" th
```

Output: `/opt/meeting-bot/{recordings,transcripts,summaries}/<name>_<timestamp>.*`

---

## 3. Individual stages (run by hand / debug one piece)

### Stage 1 — Record a live meeting only
```bash
sudo -H ./screen/record_screen.sh "<meeting_url>" "Meeting Name" ["Display Name"]
```
Output: `/opt/meeting-bot/recordings/<name>_<timestamp>.mp4`
Kill switch: `Ctrl+\` in that terminal, or from anywhere: `sudo ./kill_meeting.sh`

### Stage 2 — Transcribe only
```bash
sudo -H ./transcribe/transcribe.sh <file_or_youtube_url> "<name>" [language]
```
- Local file (wav/mp4/m4a/mkv/webm/ogg) → AssemblyAI
- YouTube URL → youtube-transcript.io captions (fast, no audio download)

Output: `/opt/meeting-bot/transcripts/<name>_<timestamp>.{txt,srt}`

Direct client calls (no shell wrapper), useful for debugging one backend:
```bash
# AssemblyAI directly
/opt/meeting-bot-venv/bin/python3 -m transcribe.assemblyai_client <path> [language]
# or from inside transcribe/:
python3 assemblyai_client.py <path> [language]

# YouTube captions directly
python3 transcribe/yt_transcript_client.py "<youtube_url>"
```

### Stage 3 — Summarize only
```bash
/opt/meeting-bot-venv/bin/python3 ./summarize/summarize.py <video_path_or_youtube_url> <transcript.txt> [output.md]
```
No sudo needed — pure CPU/network. If `output.md` is omitted, check the
script's own default path behavior (see `summarize/summarize.py` docstring).

Env var to change backend for this run only:
```bash
SUMMARY_BACKEND=anthropic /opt/meeting-bot-venv/bin/python3 ./summarize/summarize.py ...
```

### Frame extraction only (normally called internally by summarize.py)
```bash
python3 screen/extract_frames.py <video_path> <output_dir> ["meeting_name"]
```
Output: PNGs + `manifest.json` in `<output_dir>` (or `FRAME_OUTPUT_DIR`, default `/opt/meeting-bot/frames`).

---

## 4. Kill a running recording

```bash
sudo ./kill_meeting.sh
```
Signals the bot to click "Leave" in the meeting UI, then SIGTERMs the
recorder/transcribe/summarize processes it finds running.

---

## 5. Remote trigger (start a recording from your phone via Tailscale)

One-time setup:
```bash
sudo cp meeting-bot-trigger.service /etc/systemd/system/
sudo cp trigger_server.py /opt/meeting-bot/trigger_server.py
echo "MEETING_BOT_TOKEN=$(openssl rand -hex 24)" | sudo tee /etc/meeting-bot.env
sudo systemctl daemon-reload
sudo systemctl enable --now meeting-bot-trigger
sudo ufw allow in on tailscale0 to any port 8765 proto tcp
sudo ufw deny 8765
```

Trigger a run:
```bash
curl -X POST http://<tailscale-hostname>:8765/trigger \
  -H "Authorization: Bearer <token from /etc/meeting-bot.env>" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://meet.google.com/abc-defg-hij", "name": "Client Call"}'
```
Returns `202` immediately; `pipeline.sh` runs in the background on the VM.
This only accepts meeting/YouTube URLs today (it shells out to `pipeline.sh`
with the `url` field verbatim) — it does not currently accept a local file
path over the wire, since there's nothing to upload it from remotely.

Service management:
```bash
sudo systemctl status meeting-bot-trigger
sudo systemctl restart meeting-bot-trigger
journalctl -u meeting-bot-trigger -f
```

---

## 6. Config

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```
Loaded by `pipeline.sh`, `transcribe.sh`, and `summarize.py` via `source_env.sh`.
Already-exported env vars win over `.env`, so one-off overrides work too:
```bash
SUMMARY_BACKEND=ollama sudo -H ./pipeline.sh "<url>" "Meeting Name"
```
