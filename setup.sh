#!/bin/bash
# One-time setup for the meeting recording + transcription bot on a server VM:
# Debian 13 (trixie) on Proxmox (LXC container or KVM VM), 4 vCPU / 8GB, run as root, state under /opt/meeting-bot, real Google
# Chrome, systemd units.
#
# This is the `debian13-in-proxmox` branch. The same code runs on a desktop PC as a
# normal user with Firefox ESR and pm2 on the `debian13` branch; only the
# deployment (this script, the units, .env.example's paths, webui.sh) differs.
#
#   sudo -H ./setup.sh [flags]
#
# Idempotent: re-running is safe and cheap. Flags:
#   --no-chrome          skip Chrome + the browser drivers (stages 2 and 3 only)
#   --with-firefox       also install Firefox ESR + geckodriver
#                        (MEETING_BROWSER=firefox-esr; Chrome stays the default)
#   --with-libreoffice   also install LibreOffice, so .pptx slides passed via
#                        --resources can be rendered into the PDF (~700MB)
#   --with-trigger       install and enable the web UI (meeting-bot-trigger.service)
#   --with-resume-timer  install the 15-minute `pipeline.sh --resume-all` timer,
#                        so a run paused on the Claude usage window finishes
#                        without anyone watching
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_CHROME=1
INSTALL_FIREFOX=0
INSTALL_LIBREOFFICE=0
INSTALL_TRIGGER=0
INSTALL_RESUME_TIMER=0
DISTRO_NAME="Debian 13"

for arg in "$@"; do
  case "$arg" in
    --no-chrome)         INSTALL_CHROME=0 ;;
    --with-firefox)      INSTALL_FIREFOX=1 ;;
    --with-libreoffice)  INSTALL_LIBREOFFICE=1 ;;
    --with-trigger)      INSTALL_TRIGGER=1 ;;
    --with-resume-timer) INSTALL_RESUME_TIMER=1 ;;
    -h|--help)           sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown flag: $arg (try --help)" >&2; exit 1 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "This must be run as root — try: sudo -H $0" >&2
  exit 1
fi
if ! command -v apt-get >/dev/null 2>&1; then
  echo "ERROR: apt-get not found. This setup script targets $DISTRO_NAME." >&2
  exit 1
fi
. /etc/os-release 2>/dev/null || true
case "${ID:-} ${ID_LIKE:-}" in
  *debian*|*ubuntu*) ;;
  *) echo "WARNING: this looks like ${PRETTY_NAME:-an unknown distro}, not $DISTRO_NAME."
     echo "  Continuing anyway — the package names below are Debian's." ;;
esac
export DEBIAN_FRONTEND=noninteractive

LOCAL_BIN=/usr/local/bin
VENV="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}"

echo "==> Updating the package index"
apt-get update -qq

echo "==> Installing packages"
# ffmpeg            recording (x11grab + pulse), frames, audio demux, clips
# xvfb              the hidden display the recorder's browser renders into
# x11vnc/novnc      first_time_login.sh --novnc (the VM has no screen)
# pulseaudio        a server VM has no audio stack: lib/xsession.sh starts the
#                   daemon on demand and loads one null sink per run
# poppler-utils     pdftotext/pdftoppm for --resources PDFs and slide images
# libpango*         WeasyPrint's text shaping — without it the PDF export dies
# fonts-*           Thai in the browser (locale th-TH) and in the PDF
apt-get install -y --no-install-recommends \
  ca-certificates curl wget gnupg git unzip \
  ffmpeg \
  python3 python3-venv \
  procps psmisc util-linux \
  tzdata locales \
  xvfb x11vnc novnc websockify \
  pulseaudio pulseaudio-utils \
  poppler-utils \
  libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
  fonts-thai-tlwg fonts-liberation fonts-noto-core fonts-cmu \
  fontconfig
# JetBrains Mono (the code face, DESIGN.md) is packaged on Debian 13; where it
# is not, the PDF falls back to the next monospace face.
apt-get install -y --no-install-recommends fonts-jetbrains-mono 2>/dev/null \
  || echo "  fonts-jetbrains-mono not available here — code blocks use the fallback face."

echo "==> Generating locales (en_US.UTF-8, th_TH.UTF-8)"
sed -i 's/^# *\(en_US.UTF-8\|th_TH.UTF-8\)/\1/' /etc/locale.gen 2>/dev/null || true
locale-gen >/dev/null 2>&1 || locale-gen en_US.UTF-8 th_TH.UTF-8 >/dev/null 2>&1 || true

if [ "$INSTALL_LIBREOFFICE" -eq 1 ]; then
  echo "==> Installing LibreOffice Impress (for .pptx -> PDF -> slide images)"
  apt-get install -y --no-install-recommends libreoffice-impress
fi

if [ "$INSTALL_CHROME" -eq 1 ]; then
  echo "==> Installing real Google Chrome (the default MEETING_BROWSER here)"
  # NOT chromium. Google's sign-in flow blocks unbranded Chromium builds with
  # "This browser or app may not be secure". See CLAUDE.md.
  if [ ! -f /usr/share/keyrings/google-chrome.gpg ]; then
    wget -q -O /tmp/google-signing-key.pub https://dl.google.com/linux/linux_signing_key.pub
    gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg /tmp/google-signing-key.pub
    rm -f /tmp/google-signing-key.pub
  fi
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
  apt-get update -qq
  apt-get install -y --no-install-recommends google-chrome-stable
  google-chrome-stable --version
fi

if [ "$INSTALL_FIREFOX" -eq 1 ]; then
  echo "==> Installing Firefox ESR (MEETING_BROWSER=firefox-esr)"
  apt-get install -y --no-install-recommends firefox-esr
fi

echo "==> uv -> $LOCAL_BIN"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$LOCAL_BIN" UV_NO_MODIFY_PATH=1 sh
fi
uv --version

echo "==> Python venv at $VENV (uv)"
# --seed keeps a pip inside for README's troubleshooting steps. The lockfiles
# are compiled for Python 3.13: the system one where it is 3.13, otherwise a
# uv-managed 3.13 is downloaded.
if [ ! -x "$VENV/bin/python3" ]; then
  uv venv --seed --python 3.13 "$VENV"
fi
REQS=(-r "$SCRIPT_DIR/requirements.txt")
if [ "$INSTALL_CHROME" -eq 1 ] || [ "$INSTALL_FIREFOX" -eq 1 ]; then
  REQS+=(-r "$SCRIPT_DIR/requirements-browser.txt")
fi
uv pip install --quiet --python "$VENV/bin/python3" "${REQS[@]}"
if [ "$INSTALL_CHROME" -eq 1 ]; then
  # The shared libraries Chrome needs. NOT `playwright install chromium`:
  # capture.py drives the package above with channel="chrome".
  "$VENV/bin/playwright" install-deps chromium >/dev/null 2>&1 || true
fi

if [ "$INSTALL_FIREFOX" -eq 1 ] && ! command -v geckodriver >/dev/null 2>&1; then
  echo "==> geckodriver -> $LOCAL_BIN"
  tag="$(curl -fsSL -o /dev/null -w '%{url_effective}' \
         https://github.com/mozilla/geckodriver/releases/latest | sed 's#.*/tag/##')"
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/gd.tgz" \
    "https://github.com/mozilla/geckodriver/releases/download/$tag/geckodriver-$tag-linux64.tar.gz"
  tar -xzf "$tmp/gd.tgz" -C "$tmp"
  install -m 0755 "$tmp/geckodriver" "$LOCAL_BIN/geckodriver"
  rm -rf "$tmp"
fi

echo "==> yt-dlp -> $LOCAL_BIN"
# From GitHub releases, not apt: YouTube breaks yt-dlp regularly and a stale
# binary is the #1 cause of silent failures. Refreshed on every run of this.
curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o "$LOCAL_BIN/yt-dlp"
chmod a+rx "$LOCAL_BIN/yt-dlp"
"$LOCAL_BIN/yt-dlp" --version

echo "==> deno (yt-dlp's JavaScript runtime) -> $LOCAL_BIN"
# yt-dlp solves YouTube's JS challenges with an external runtime; without one
# some videos are "not available". See CLAUDE.md, "yt-dlp needs deno".
if ! command -v deno >/dev/null 2>&1; then
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/deno.zip" \
    https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip
  python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extract("deno", sys.argv[2])' \
    "$tmp/deno.zip" "$tmp"
  install -m 0755 "$tmp/deno" "$LOCAL_BIN/deno"
  rm -rf "$tmp"
fi
deno --version | head -n 1

echo "==> Vendored Thai PDF fonts (Bai Jamjuree, Sarabun) -> /usr/local/share/fonts/meeting-bot"
install -d /usr/local/share/fonts/meeting-bot
install -m 0644 "$SCRIPT_DIR"/fonts/*/*.ttf /usr/local/share/fonts/meeting-bot/
fc-cache -f /usr/local/share/fonts/meeting-bot >/dev/null 2>&1 || true

echo "==> .env"
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
  chmod 600 "$SCRIPT_DIR/.env"
  echo "  created from .env.example — fill in BOT_GOOGLE_ACCOUNT and your keys."
fi
if ! grep -qE '^MEETING_BOT_TOKEN=.+' "$SCRIPT_DIR/.env"; then
  token="$("$VENV/bin/python3" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  if grep -qE '^MEETING_BOT_TOKEN=' "$SCRIPT_DIR/.env"; then
    sed -i "s|^MEETING_BOT_TOKEN=.*|MEETING_BOT_TOKEN=$token|" "$SCRIPT_DIR/.env"
  else
    printf '\nMEETING_BOT_TOKEN=%s\n' "$token" >> "$SCRIPT_DIR/.env"
  fi
  echo "  generated MEETING_BOT_TOKEN for the web UI."
fi

echo "==> Working directories"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"
mkdir -p "$MEETING_BOT_ROOT"/{runs,tmp,state,logs,resources}
for var in RECORDINGS_DIR TRANSCRIPTS_DIR FRAMES_DIR SUMMARIES_DIR PDF_DIR; do
  dir="${!var:-}"
  [ -n "$dir" ] || { echo "  $var is not set in .env"; continue; }
  mkdir -p "$dir" 2>/dev/null && echo "  $var = $dir" \
    || echo "  WARNING: cannot create $var ($dir) — is the drive mounted?"
done

install_unit() {  # <unit file in the repo>
  local name; name="$(basename "$1")"
  sed -e "s|@REPO_ROOT@|$SCRIPT_DIR|g" -e "s|@VENV@|$VENV|g" "$1" \
    > "/etc/systemd/system/$name"
  echo "  /etc/systemd/system/$name"
}
if [ "$INSTALL_TRIGGER" -eq 1 ] || [ "$INSTALL_RESUME_TIMER" -eq 1 ]; then
  echo "==> systemd units"
  [ "$INSTALL_TRIGGER" -eq 1 ] && install_unit "$SCRIPT_DIR/meeting-bot-trigger.service"
  if [ "$INSTALL_RESUME_TIMER" -eq 1 ]; then
    install_unit "$SCRIPT_DIR/meeting-bot-resume.service"
    install_unit "$SCRIPT_DIR/meeting-bot-resume.timer"
  fi
  systemctl daemon-reload
  [ "$INSTALL_TRIGGER" -eq 1 ] && systemctl enable --now meeting-bot-trigger.service
  [ "$INSTALL_RESUME_TIMER" -eq 1 ] && systemctl enable --now meeting-bot-resume.timer
fi

echo "==> The claude CLI (the summarizer spends your Claude subscription)"
CLI="${CLAUDE_CLI_BIN:-$(command -v claude || true)}"
if [ -n "$CLI" ] && [ -x "$CLI" ]; then
  echo "  $CLI ($("$CLI" --version 2>/dev/null | head -n 1))"
else
  echo "  not installed. Install it, sign in, and set CLAUDE_CLI_BIN in .env:"
  echo "      curl -fsSL https://claude.ai/install.sh | bash"
  echo "      /root/.local/bin/claude auth login"
fi

echo ""
echo "==> Done."
echo "Next steps:"
echo "  1. Edit .env: BOT_GOOGLE_ACCOUNT, your API keys, and the five output directories."
echo "  2. ./first_time_login.sh --novnc   — sign the bot's Chrome profile in, over noVNC."
echo "  3. ./pipeline.sh <url-or-file>     — a Meet/Zoom link records in the background."
echo "  4. ./webui.sh on                   — the web UI (systemd; --with-trigger also starts it at boot)."
echo ""
echo "Check the configuration with:"
echo "  $VENV/bin/python3 lib/paths.py show"
echo "  ./verify_e2e.sh --preflight"
