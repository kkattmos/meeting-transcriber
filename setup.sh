#!/bin/bash
# One-time setup for the meeting recording + transcription bot, on a Debian 13
# (trixie) desktop PC. Everything runs as YOUR user — not root — so setup is
# two steps:
#
#   sudo ./setup.sh --system [--with-chrome] [--with-libreoffice]
#       apt packages only: ffmpeg, Xvfb, pactl, noVNC, Firefox ESR, the PDF
#       libraries and fonts, locales. --with-chrome adds google-chrome-stable
#       (only needed for MEETING_BROWSER=chrome); --with-libreoffice lets
#       .pptx slides passed via --resources be rendered into the PDF (~700MB).
#
#   ./setup.sh [--no-browser]
#       everything per-user, no sudo: uv, the project venv (.venv, built with
#       uv — `rm -rf .venv` removes every Python dependency), geckodriver and
#       yt-dlp in ~/.local/bin, the vendored Thai fonts in ~/.local/share/fonts,
#       pm2 (npm, into ~/.local), and a .env with a fresh web UI token if you
#       don't have one yet. --no-browser skips the recorder's Python drivers.
#
# Idempotent: re-running either step is safe and cheap.
#
# The Proxmox VM version (root, /opt, systemd units) is preserved on the
# `debian13-in-proxmox` branch; see CLAUDE.md.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
MODE="user"
INSTALL_CHROME=0
INSTALL_LIBREOFFICE=0
INSTALL_BROWSER_DEPS=1

for arg in "$@"; do
  case "$arg" in
    --system)           MODE="system" ;;
    --with-chrome)      INSTALL_CHROME=1 ;;
    --with-libreoffice) INSTALL_LIBREOFFICE=1 ;;
    --no-browser)       INSTALL_BROWSER_DEPS=0 ;;
    -h|--help)          sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown flag: $arg (try --help)" >&2; exit 1 ;;
  esac
done

# ============================================================================
# System step (root)
# ============================================================================
if [ "$MODE" = "system" ]; then
  if [ "$(id -u)" -ne 0 ]; then
    echo "The --system step installs apt packages — run it with sudo:" >&2
    echo "    sudo ./setup.sh --system" >&2
    exit 1
  fi
  if ! command -v apt-get >/dev/null 2>&1; then
    echo "ERROR: apt-get not found. This setup script targets Debian 13." >&2
    exit 1
  fi
  . /etc/os-release 2>/dev/null || true
  if [ "${ID:-}" != "debian" ] && [ "${ID_LIKE:-}" != "debian" ]; then
    echo "WARNING: this looks like ${PRETTY_NAME:-an unknown distro}, not Debian."
    echo "  Continuing anyway — the package names below are Debian's."
  fi
  export DEBIAN_FRONTEND=noninteractive

  echo "==> Updating the package index"
  apt-get update -qq

  echo "==> Installing packages"
  # ffmpeg            recording (x11grab + pulse), frames, audio demux, clips
  # xvfb              the hidden display the recorder's browser renders into —
  #                   never your desktop, so a meeting doesn't take over the screen
  # x11vnc/novnc      first_time_login.sh --novnc (remote login)
  # pulseaudio-utils  pactl, for the per-run null sinks. NOT the pulseaudio
  #                   daemon: Debian's desktop runs PipeWire, whose pulse
  #                   socket answers pactl, and the daemon would fight it
  # firefox-esr       the recorder's default browser (MEETING_BROWSER)
  # poppler-utils     pdftotext/pdftoppm for --resources PDFs and slide images
  # libpango*         WeasyPrint's text shaping — without it the PDF export dies
  # fonts-*           Thai in the browser (locale th-TH) and in the PDF
  # nodejs/npm        pm2, which runs the web UI
  apt-get install -y --no-install-recommends \
    ca-certificates curl wget gnupg git \
    ffmpeg \
    python3 python3-venv \
    procps psmisc util-linux \
    tzdata locales \
    xvfb x11vnc novnc websockify \
    pulseaudio-utils \
    firefox-esr \
    poppler-utils \
    libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b \
    fonts-thai-tlwg fonts-liberation fonts-noto-core fonts-cmu \
    nodejs npm

  # fonts-cmu is CMU Serif — Computer Modern, the PDF's body face for English
  # summaries and the face mathtext sets the maths in (see summarize/pdf.py).
  # fonts-noto-core carries Noto Serif Thai, the last-resort Thai fallback.
  # Bai Jamjuree and Sarabun (the Thai body faces) are vendored under fonts/
  # and installed per user by the user step.

  echo "==> Generating locales (en_US.UTF-8, th_TH.UTF-8)"
  sed -i 's/^# *\(en_US.UTF-8\|th_TH.UTF-8\)/\1/' /etc/locale.gen
  locale-gen >/dev/null

  if [ "$INSTALL_LIBREOFFICE" -eq 1 ]; then
    echo "==> Installing LibreOffice Impress (for .pptx -> PDF -> slide images)"
    apt-get install -y --no-install-recommends libreoffice-impress
  fi

  if [ "$INSTALL_CHROME" -eq 1 ]; then
    echo "==> Installing real Google Chrome (MEETING_BROWSER=chrome)"
    # NOT chromium. Google's sign-in flow blocks unbranded Chromium builds with
    # "This browser or app may not be secure". See CLAUDE.md.
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

  echo ""
  echo "==> System packages done. Now, as yourself (no sudo):"
  echo "    ./setup.sh"
  exit 0
fi

# ============================================================================
# User step (no root)
# ============================================================================
if [ "$(id -u)" -eq 0 ]; then
  echo "ERROR: the user step must not run as root — everything it creates" >&2
  echo "  (.venv, ~/.local, the bot's state) belongs to the account that runs" >&2
  echo "  the bot. Run:  ./setup.sh     (and  sudo ./setup.sh --system  once)" >&2
  exit 1
fi

LOCAL_BIN="$HOME/.local/bin"
mkdir -p "$LOCAL_BIN"
case ":$PATH:" in
  *":$LOCAL_BIN:"*) ;;
  *) export PATH="$LOCAL_BIN:$PATH"
     echo "NOTE: $LOCAL_BIN is not on your PATH; add it to your shell profile." ;;
esac

missing=()
for cmd in ffmpeg Xvfb pactl pdftotext; do
  command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
done
if [ "${#missing[@]}" -gt 0 ]; then
  echo "WARNING: not installed yet: ${missing[*]}"
  echo "  Run  sudo ./setup.sh --system  first (or after this) — recording needs them."
fi

echo "==> uv"
# uv is the project's installer and venv manager. Not in Debian's archive, so
# it comes from astral.sh, into ~/.local/bin, no root involved.
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
fi
uv --version

VENV="${MEETING_BOT_VENV:-$SCRIPT_DIR/.venv}"
echo "==> Python venv at $VENV (uv)"
# --seed puts pip inside, which README's troubleshooting steps use. Debian's
# own python3 (3.13) — the version the lockfiles are compiled for.
if [ ! -x "$VENV/bin/python3" ]; then
  uv venv --seed --python-preference only-system --python 3.13 "$VENV"
fi
# Pinned with hashes in requirements*.txt, generated from requirements*.in —
# see requirements.in for the regenerate command.
REQS=(-r "$SCRIPT_DIR/requirements.txt")
[ "$INSTALL_BROWSER_DEPS" -eq 1 ] && REQS+=(-r "$SCRIPT_DIR/requirements-browser.txt")
uv pip install --quiet --python "$VENV/bin/python3" "${REQS[@]}"

if [ "$INSTALL_BROWSER_DEPS" -eq 1 ]; then
  echo "==> geckodriver (Firefox ESR's driver) -> $LOCAL_BIN"
  # From Mozilla's GitHub releases: Debian doesn't package it. Selenium Manager
  # could fetch one at run time, but that needs the network at the moment a
  # meeting starts; this doesn't.
  if ! command -v geckodriver >/dev/null 2>&1; then
    tag="$(curl -fsSL -o /dev/null -w '%{url_effective}' \
           https://github.com/mozilla/geckodriver/releases/latest | sed 's#.*/tag/##')"
    tmp="$(mktemp -d)"
    curl -fsSL -o "$tmp/gd.tgz" \
      "https://github.com/mozilla/geckodriver/releases/download/$tag/geckodriver-$tag-linux64.tar.gz"
    tar -xzf "$tmp/gd.tgz" -C "$tmp"
    install -m 0755 "$tmp/geckodriver" "$LOCAL_BIN/geckodriver"
    rm -rf "$tmp"
  fi
  geckodriver --version | head -n 1
fi

echo "==> yt-dlp -> $LOCAL_BIN"
# From GitHub releases, not apt: YouTube breaks yt-dlp regularly and a stale
# binary is the #1 cause of silent failures. Refreshed on every run of this.
curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
  -o "$LOCAL_BIN/yt-dlp"
chmod a+rx "$LOCAL_BIN/yt-dlp"
# A firewall that returns an HTML error page instead of the binary fails here
# rather than three stages into a real run.
"$LOCAL_BIN/yt-dlp" --version

echo "==> Vendored Thai PDF fonts (Bai Jamjuree, Sarabun) -> ~/.local/share/fonts"
# Not in Debian's archive; OFL Google Fonts vendored under fonts/. fontconfig
# (and so WeasyPrint) reads the per-user font directory.
FONT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/fonts/meeting-bot"
install -d "$FONT_DIR"
install -m 0644 "$SCRIPT_DIR"/fonts/*/*.ttf "$FONT_DIR/"
fc-cache -f "$FONT_DIR" >/dev/null 2>&1 || true

echo "==> pm2 (runs the web UI; nothing starts at boot)"
if ! command -v pm2 >/dev/null 2>&1; then
  if command -v npm >/dev/null 2>&1; then
    npm install -g --prefix "$HOME/.local" pm2 >/dev/null
  else
    echo "  npm not found — pm2 skipped (sudo ./setup.sh --system installs npm)."
  fi
fi
command -v pm2 >/dev/null 2>&1 && echo "  pm2 $(pm2 --version 2>/dev/null | tail -n 1)"

echo "==> .env"
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
  chmod 600 "$SCRIPT_DIR/.env"
  echo "  created from .env.example — fill in BOT_GOOGLE_ACCOUNT and your keys."
fi
# The web UI's shared secret: generated once, never overwritten.
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

echo "==> The claude CLI (the summarizer spends your Claude subscription)"
CLI="${CLAUDE_CLI_BIN:-$(command -v claude || true)}"
if [ -n "$CLI" ] && [ -x "$CLI" ]; then
  echo "  $CLI ($("$CLI" --version 2>/dev/null | head -n 1))"
  echo "  signed in? $(env -u ANTHROPIC_API_KEY -u ANTHROPIC_BASE_URL "$CLI" auth status 2>/dev/null \
                     | grep -o '"loggedIn": *[a-z]*' || echo unknown)"
else
  echo "  not installed. Install it, sign in, and set CLAUDE_CLI_BIN in .env:"
  echo "      curl -fsSL https://claude.ai/install.sh | bash"
  echo "      ~/.local/bin/claude auth login"
fi

echo ""
echo "==> Done."
echo "Next steps:"
echo "  1. Edit .env: BOT_GOOGLE_ACCOUNT (the bot's Google address), your API keys,"
echo "     and the five output directories."
echo "  2. ./first_time_login.sh        — sign the bot's browser profile in as"
echo "                                    BOT_GOOGLE_ACCOUNT (a window on this desktop)."
echo "  3. ./pipeline.sh <url-or-file>  — a Meet/Zoom link records in the background."
echo "  4. ./webui.sh on                — the web UI under pm2 (off by default)."
echo ""
echo "Check the configuration with:"
echo "  $VENV/bin/python3 lib/paths.py show"
echo "  $VENV/bin/python3 lib/keyring.py status"
echo "  ./verify_e2e.sh --preflight"
