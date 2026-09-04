#!/bin/bash
# One-time setup for the meeting recording + transcription bot.
# Target: Debian 13 (trixie) on Proxmox (LXC container or KVM VM), 4 vCPU / 8GB.
#
# Everything runs natively on this host — there is no container split any more.
# Debian is glibc, so Google's own google-chrome-stable package installs and
# runs here; the Alpine branch needed a Debian container purely because Chrome
# has no musl build. See CLAUDE.md.
#
# Idempotent: re-running is safe and cheap. Flags:
#   --no-chrome          skip Chrome + Playwright (stages 2 and 3 only box)
#   --with-libreoffice   also install LibreOffice, so .pptx slides passed via
#                        --resources can be rendered into the PDF (~700MB)
#   --with-trigger       install and enable the systemd trigger service
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_CHROME=1
INSTALL_LIBREOFFICE=0
INSTALL_TRIGGER=0

for arg in "$@"; do
  case "$arg" in
    --no-chrome)        INSTALL_CHROME=0 ;;
    --with-libreoffice) INSTALL_LIBREOFFICE=1 ;;
    --with-trigger)     INSTALL_TRIGGER=1 ;;
    -h|--help)
      sed -n '2,16p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown flag: $arg (try --help)" >&2
      exit 1
      ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "This must be run as root — try: sudo -H $0" >&2
  exit 1
fi

if ! command -v apt-get >/dev/null 2>&1; then
  echo "ERROR: apt-get not found. This setup script targets Debian 13." >&2
  echo "  The Alpine variant lives on the 'alpinelinux' branch; see CLAUDE.md" >&2
  echo "  for why the project moved back to a glibc host." >&2
  exit 1
fi

. /etc/os-release 2>/dev/null || true
if [ "${ID:-}" != "debian" ] && [ "${ID_LIKE:-}" != "debian" ]; then
  echo "WARNING: this looks like ${PRETTY_NAME:-an unknown distro}, not Debian."
  echo "  Continuing anyway — the package names below are Debian's."
fi

export DEBIAN_FRONTEND=noninteractive
VENV="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}"

# Read the operator's own directory choices if .env already exists, so setup
# creates the directories they actually configured rather than the defaults.
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh" || true
MEETING_BOT_ROOT="${MEETING_BOT_ROOT:-/opt/meeting-bot}"
RECORDINGS_DIR="${RECORDINGS_DIR:-$MEETING_BOT_ROOT/recordings}"
TRANSCRIPTS_DIR="${TRANSCRIPTS_DIR:-$MEETING_BOT_ROOT/transcripts}"
FRAMES_DIR="${FRAMES_DIR:-$MEETING_BOT_ROOT/frames}"
SUMMARIES_DIR="${SUMMARIES_DIR:-$MEETING_BOT_ROOT/summaries}"
PDF_DIR="${PDF_DIR:-$MEETING_BOT_ROOT/pdf}"
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-$MEETING_BOT_ROOT/chrome-profile}"
RESOURCE_CACHE_DIR="${RESOURCE_CACHE_DIR:-$MEETING_BOT_ROOT/resources}"

echo "==> Updating the package index"
apt-get update -qq

echo "==> Installing base tools"
# ffmpeg           frame extraction (stage 3), audio demux (stage 2), x11grab
# xvfb             the virtual display the browser renders into
# x11vnc/novnc     the viewable-browser path used by first_time_login.sh
# pulseaudio       per-run null sinks; ffmpeg records their .monitor source
# poppler-utils    pdftotext/pdftoppm for --resources PDFs and slide images
# libpango*        WeasyPrint's text shaping — without it the PDF export dies
# fonts-*          Thai renders in both the browser (locale th-TH) and the PDF
apt-get install -y --no-install-recommends \
  ca-certificates curl wget gnupg git \
  ffmpeg \
  python3 python3-venv python3-dev \
  build-essential pkg-config \
  procps psmisc \
  tzdata locales \
  xvfb x11vnc novnc websockify \
  pulseaudio pulseaudio-utils \
  wmctrl xdotool \
  poppler-utils \
  libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libffi-dev \
  fonts-thai-tlwg fonts-liberation fonts-noto-core

# Thai locale: Chrome is driven with locale th-TH so Thai participant names
# render instead of boxes. See CLAUDE.md — this is deliberate, not cosmetic.
echo "==> Generating locales (en_US.UTF-8, th_TH.UTF-8)"
sed -i 's/^# *\(en_US.UTF-8\|th_TH.UTF-8\)/\1/' /etc/locale.gen
locale-gen >/dev/null

if [ "$INSTALL_LIBREOFFICE" -eq 1 ]; then
  echo "==> Installing LibreOffice Impress (for .pptx -> PDF -> slide images)"
  apt-get install -y --no-install-recommends libreoffice-impress
fi

if [ "$INSTALL_CHROME" -eq 1 ]; then
  echo "==> Installing real Google Chrome"
  # NOT chromium. Google's sign-in flow blocks unbranded Chromium builds with
  # "This browser or app may not be secure", which is the entire reason this
  # project insists on the branded package. See CLAUDE.md.
  if [ ! -f /usr/share/keyrings/google-chrome.gpg ]; then
    wget -q -O /tmp/google-signing-key.pub \
      https://dl.google.com/linux/linux_signing_key.pub
    gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
      /tmp/google-signing-key.pub
    rm -f /tmp/google-signing-key.pub
  fi
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
    > /etc/apt/sources.list.d/google-chrome.list
  apt-get update -qq
  apt-get install -y --no-install-recommends google-chrome-stable
  google-chrome-stable --version
fi

echo "==> Setting up the Python venv at $VENV"
# --system-site-packages is deliberately NOT used: we want a self-contained
# venv so an apt upgrade of python3-* can't silently change SDK versions.
if [ ! -x "$VENV/bin/python3" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip

# anthropic:    the default summary backend.
# google-genai: Gemini, the fallback backend.
# requests:     youtube-transcript.io client (urllib gets rate-limited where
#               requests doesn't — different User-Agent and TLS stack).
# assemblyai:   pre-recorded transcription API for local files.
# weasyprint,
# markdown,
# pillow:       the PDF export and its frame cropping.
PY_DEPS="anthropic google-genai requests assemblyai weasyprint markdown pillow"
if [ "$INSTALL_CHROME" -eq 1 ]; then
  PY_DEPS="$PY_DEPS playwright"
fi
echo "==> Installing Python dependencies"
"$VENV/bin/pip" install --quiet --no-cache-dir $PY_DEPS

if [ "$INSTALL_CHROME" -eq 1 ]; then
  echo "==> Installing Chrome's shared libraries for Playwright"
  # `playwright install-deps chromium` pulls the shared libraries Chrome needs.
  # We deliberately do NOT `playwright install chromium` (the bundled browser):
  # capture.py uses channel="chrome" against the Google package above.
  "$VENV/bin/playwright" install-deps chromium
fi

echo "==> Installing the latest yt-dlp"
# From GitHub releases, not apt: YouTube breaks yt-dlp regularly and a stale
# binary is the #1 cause of silent failures.
curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
  -o /usr/local/bin/yt-dlp
chmod a+rx /usr/local/bin/yt-dlp
# A firewall that returns an HTML error page instead of the binary fails here
# rather than three stages into a real run.
/usr/local/bin/yt-dlp --version

echo "==> Creating working directories"
# The five media directories are configured independently in .env; the rest is
# the pipeline's own bookkeeping under MEETING_BOT_ROOT.
mkdir -p "$RECORDINGS_DIR" "$TRANSCRIPTS_DIR" "$FRAMES_DIR" \
         "$SUMMARIES_DIR" "$PDF_DIR" "$RESOURCE_CACHE_DIR" \
         "$MEETING_BOT_ROOT/runs" "$MEETING_BOT_ROOT/tmp" \
         "$MEETING_BOT_ROOT/state" "$CHROME_PROFILE_DIR"

if [ "$INSTALL_TRIGGER" -eq 1 ]; then
  echo "==> Installing the systemd trigger service"
  # Debian has systemd, so the unit that the Alpine branch kept purely for
  # reference is usable again.
  sed -e "s#@REPO_ROOT@#$SCRIPT_DIR#g" -e "s#@VENV@#$VENV#g" \
    "$SCRIPT_DIR/meeting-bot-trigger.service" \
    > /etc/systemd/system/meeting-bot-trigger.service
  systemctl daemon-reload
  systemctl enable --now meeting-bot-trigger.service
  systemctl --no-pager status meeting-bot-trigger.service || true
fi

echo ""
echo "==> Done."
echo "Next steps:"
echo "  1. cp .env.example .env && chmod 600 .env  — then fill in your API keys."
echo "     (Anthropic key, Gemini keys 1-3, AssemblyAI keys 1-3,"
echo "      youtube-transcript.io keys 1-10, and the five output directories.)"
echo "  2. ./first_time_login.sh   — sign into Google/Zoom in the persistent"
echo "     Chrome profile. It prints an SSH-tunnel command you run from your"
echo "     own machine, since this box has no visible display."
echo "  3. ./pipeline.sh <url-or-file>   — record, transcribe, summarize."
echo ""
echo "Check the configuration with:"
echo "  $VENV/bin/python3 lib/paths.py show"
echo "  $VENV/bin/python3 lib/keyring.py status"
