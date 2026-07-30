#!/bin/bash
# One-time setup for the meeting recording + transcription bot.
# Target: Ubuntu 26.04 LTS Server (minimized cloud image recommended),
# 4 vCPU / 8GB RAM Proxmox VM.
set -e

echo "==> Updating system"
apt update && apt upgrade -y

echo "==> Installing base tools"
apt install -y \
  build-essential git wget curl unzip ffmpeg \
  xvfb x11vnc novnc websockify \
  pulseaudio pulseaudio-utils \
  python3 python3-venv python3-pip \
  cmake wmctrl xdotool \
  locales fonts-thai-tlwg

echo "==> Enabling Thai locale (for correct rendering/sorting of Thai text)"
locale-gen th_TH.UTF-8 en_US.UTF-8
update-locale LANG=en_US.UTF-8

echo "==> Setting up Python venv + Playwright"
python3 -m venv /opt/meeting-bot-venv
source /opt/meeting-bot-venv/bin/activate
pip install --upgrade pip
# anthropic: SDK for the Anthropic API (also used against the FCC proxy).
# openai: SDK for NVIDIA NIM (and any other OpenAI-compatible chat-completions
#         endpoint). Both NIM's hosted service and self-hosted NIM speak the
#         OpenAI protocol, so this one dependency covers both.
# google-genai: SDK for Google Gemini summaries (transitive dep
#         google-generativeai provides the legacy API used inside llm_client).
# requests: HTTP client for transcribe/yt_transcript_client.py
#         (youtube-transcript.io API). urllib was being rate-limited where
#         curl worked; requests sends a different User-Agent and TLS stack.
# assemblyai: SDK for the AssemblyAI pre-recorded transcription API
#         (transcribe/assemblyai_client.py). Replaces the whisper.cpp
#         local-file path; the API accepts mp3/mp4/m4a/wav/webm/ogg and
#         handles the upload internally.
pip install playwright anthropic openai google-genai requests assemblyai
playwright install --with-deps chromium
deactivate

echo "==> Installing real Google Chrome"
# Google blocks sign-in ("This browser or app may not be secure") from
# unbranded open-source Chromium builds, like the one Playwright bundles above.
# Real Chrome is needed for the one-time manual login (first_time_login.sh)
# and is what join_meeting.py drives afterward via Playwright's channel="chrome".
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
  > /etc/apt/sources.list.d/google-chrome.list
apt update
apt install -y google-chrome-stable

echo "==> Building whisper.cpp"
cd /opt
if [ -d whisper.cpp ]; then
  echo "    /opt/whisper.cpp already exists - pulling latest instead of re-cloning."
  cd whisper.cpp
  git pull
else
  git clone https://github.com/ggerganov/whisper.cpp.git
  cd whisper.cpp
fi
# Build is cheap on incremental changes, so always run it - guarantees the
# whisper-cli binary is in sync with the source tree.
cmake -B build
cmake --build build -j"$(nproc)" --config Release

echo "==> Downloading multilingual model (small - supports Thai)"
# NOTE: use the multilingual model, NOT the '.en' English-only variant.
# 'small' is a good CPU-only balance for Thai. For noticeably better Thai
# accuracy at ~3x the time/RAM cost, switch to 'medium' here and in
# record_and_transcribe.sh's WHISPER_MODEL variable.
bash ./models/download-ggml-model.sh small

echo "==> Installing latest yt-dlp"
# Used by transcribe/transcribe.sh and summarize/summarize.py to download
# audio + video from YouTube URLs. Installing from GitHub releases (not the
# apt package) so we always get the freshest build - YouTube breaks yt-dlp
# regularly and a stale binary is the #1 cause of silent failures.
curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
  -o /usr/local/bin/yt-dlp
chmod a+rx /usr/local/bin/yt-dlp
# Sanity check: a broken download (e.g. firewall block on GitHub) returns
# an HTML page instead of a binary, which fails immediately on --version.
/usr/local/bin/yt-dlp --version

echo "==> Installing deno (JS runtime for yt-dlp's YouTube extraction)"
# yt-dlp now requires a JavaScript runtime to compute YouTube's signed
# player JS. Without one, newer formats are unavailable and the
# "bestvideo+bestaudio + merge" path silently fails. Deno is the runtime
# yt-dlp's error message recommends; it's a single self-contained binary
# with no apt dependency. Idempotent: skip if already installed.
if ! command -v deno >/dev/null 2>&1; then
  curl -fsSL -o /tmp/deno.zip \
    https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
  unzip -o /tmp/deno.zip -d /usr/local/bin/ >/dev/null
  chmod a+rx /usr/local/bin/deno
  rm -f /tmp/deno.zip
fi
# Sanity check, mirroring the yt-dlp block above.
deno --version

echo "==> Creating working directories"
mkdir -p /opt/meeting-bot/{recordings,transcripts,summaries,frames}
mkdir -p /opt/meeting-bot/secrets
mkdir -p ~/.meeting-bot/chrome-profile

# Bootstrap a starter keys file for the youtube-transcript.io API client.
# The first run of yt_transcript_client.py will populate it with an empty
# keys list; the user adds accounts by editing this file. chmod 600 is set
# by the client on first write; on existing files we just enforce permissions.
KEYS_FILE="/opt/meeting-bot/secrets/youtube_transcript_keys.json"
if [ ! -f "$KEYS_FILE" ]; then
  cat > "$KEYS_FILE" <<'EOF'
{
  "keys": [],
  "next_index": 0,
  "_comment": "Add your youtube-transcript.io API tokens to the 'keys' list. Each entry is one account. Save and re-run; transcribe.sh rotates through them in order. The cursor (next_index) is updated after each successful call."
}
EOF
  chmod 600 "$KEYS_FILE" || true
fi

echo "==> Done."
echo "Next steps:"
echo "  1. Edit $KEYS_FILE and add at least one youtube-transcript.io API token."
echo "  2. Run first_time_login.sh once to log into Google/Zoom inside the persistent browser profile."
echo "  3. Set SUMMARY_BACKEND / SUMMARY_FALLBACK_CHAIN / NIM and Gemini env vars as needed."
