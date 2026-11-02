#!/bin/bash
# One-time setup on the Alpine Linux host (the `main` branch), as root.
#
# Alpine is musl and Chrome is glibc-only, so the system is split in two:
#   - this host runs everything but the browser: the pipeline, transcribe,
#     summarize, the web UI — Python from a uv venv (musl wheels,
#     requirements-alpine.txt), ffmpeg, yt-dlp + deno, the PDF renderer;
#   - a Debian container (docker/Dockerfile.recorder) runs the browser stage:
#     the recorder (screen/record_screen.sh, unchanged) and the sign-in.
#
#   ./setup.sh [flags]          (as root; Alpine 3.20+)
#
#   --build-recorder     (re)build the recorder image (also done on first run)
#   --with-trigger       install and start the web UI as an OpenRC service
#   --with-resume-cron   install the 15-minute `pipeline.sh --resume-all` (crond)
#   --with-libreoffice   LibreOffice, so .pptx slides can be rendered into the PDF
#
# Idempotent. The Debian desktop build of the same code is the `debian13`
# branch, where features are developed first.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_RECORDER=0
INSTALL_TRIGGER=0
INSTALL_RESUME=0
INSTALL_LIBREOFFICE=0
for arg in "$@"; do
  case "$arg" in
    --build-recorder)    BUILD_RECORDER=1 ;;
    --with-trigger)      INSTALL_TRIGGER=1 ;;
    --with-resume-cron)  INSTALL_RESUME=1 ;;
    --with-libreoffice)  INSTALL_LIBREOFFICE=1 ;;
    -h|--help)           sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown flag: $arg (try --help)" >&2; exit 1 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
command -v apk >/dev/null 2>&1 || {
  echo "ERROR: apk not found — this is the Alpine branch. Debian: see debian13 /" >&2
  echo "  debian13-in-proxmox." >&2; exit 1; }

VENV="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}"

echo "==> Packages (apk)"
# bash/coreutils/procps/util-linux: the scripts use GNU options (ps --ppid,
# date -d, stat) that busybox lacks. pango + fontconfig: WeasyPrint's text
# engine. python3: yt-dlp's zipapp and the bootstrap; the venv's own Python is
# a uv-managed 3.13 (the lockfile's version).
apk add --no-cache \
  bash coreutils findutils grep sed procps util-linux tzdata \
  ca-certificates curl git \
  ffmpeg python3 \
  poppler-utils \
  pango fontconfig \
  font-noto-thai font-liberation \
  docker docker-cli-compose
apk add --no-cache deno 2>/dev/null \
  || echo "  deno is not in this Alpine's repositories (it is in community) — YouTube may refuse some videos without it."
apk add --no-cache font-jetbrains-mono 2>/dev/null \
  || echo "  font-jetbrains-mono not available — code blocks use the fallback monospace face."
[ "$INSTALL_LIBREOFFICE" -eq 1 ] && apk add --no-cache libreoffice-impress

echo "==> Docker"
rc-update add docker default >/dev/null 2>&1 || true
rc-service docker start >/dev/null 2>&1 || service docker start >/dev/null 2>&1 || true

echo "==> uv -> /usr/local/bin"
command -v uv >/dev/null 2>&1 || \
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh

echo "==> Python venv at $VENV (uv, Python 3.13 for musl)"
[ -x "$VENV/bin/python3" ] || uv venv --seed --python 3.13 "$VENV"
# requirements-alpine.txt: the same requirements.in, compiled for
# x86_64-unknown-linux-musl (musllinux wheels). The glibc lockfiles are for
# the recorder image.
uv pip install --quiet --python "$VENV/bin/python3" -r "$SCRIPT_DIR/requirements-alpine.txt"

echo "==> yt-dlp -> /usr/local/bin (refreshed every run)"
curl -fsSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
chmod a+rx /usr/local/bin/yt-dlp
/usr/local/bin/yt-dlp --version

echo "==> PDF fonts -> /usr/share/fonts/meeting-bot"
install -d /usr/share/fonts/meeting-bot
install -m 0644 "$SCRIPT_DIR"/fonts/*/*.ttf /usr/share/fonts/meeting-bot/
# CMU Serif (Computer Modern: the English body face and the maths face) is not
# packaged for Alpine; CTAN serves the OpenType files.
for f in cmunrm cmunbx cmunti cmunbi cmuntt; do
  [ -f "/usr/share/fonts/meeting-bot/$f.otf" ] && continue
  curl -fsSL -o "/usr/share/fonts/meeting-bot/$f.otf" \
    "https://mirrors.ctan.org/fonts/cm-unicode/fonts/otf/$f.otf" \
    || echo "  could not fetch $f.otf — English PDFs fall back to Liberation Serif."
done
fc-cache -f >/dev/null 2>&1 || true

echo "==> .env"
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"; chmod 600 "$SCRIPT_DIR/.env"
  echo "  created from .env.example — fill in BOT_GOOGLE_ACCOUNT and your keys."
fi
if ! grep -qE '^MEETING_BOT_TOKEN=.+' "$SCRIPT_DIR/.env"; then
  token="$("$VENV/bin/python3" -c 'import secrets; print(secrets.token_urlsafe(24))')"
  if grep -qE '^MEETING_BOT_TOKEN=' "$SCRIPT_DIR/.env"; then
    sed -i "s|^MEETING_BOT_TOKEN=.*|MEETING_BOT_TOKEN=$token|" "$SCRIPT_DIR/.env"
  else
    printf '\nMEETING_BOT_TOKEN=%s\n' "$token" >> "$SCRIPT_DIR/.env"
  fi
fi

echo "==> Working directories"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"
mkdir -p "$MEETING_BOT_ROOT"/{runs,tmp,state,logs,resources}
for var in RECORDINGS_DIR TRANSCRIPTS_DIR FRAMES_DIR SUMMARIES_DIR PDF_DIR; do
  dir="${!var:-}"; [ -n "$dir" ] && mkdir -p "$dir" && echo "  $var = $dir"
done

REPO_ROOT="$SCRIPT_DIR"; export REPO_ROOT
# shellcheck disable=SC1091
. "$SCRIPT_DIR/docker/recorder_lib.sh"
if [ "$BUILD_RECORDER" -eq 1 ] || ! docker image inspect "$RECORDER_IMAGE" >/dev/null 2>&1; then
  recorder_build
fi

[ "$INSTALL_TRIGGER" -eq 1 ] && "$SCRIPT_DIR/webui.sh" on --web-only
[ "$INSTALL_RESUME" -eq 1 ] && "$SCRIPT_DIR/webui.sh" on --resume-only

echo ""
echo "==> Done. Next:"
echo "  1. Edit .env: BOT_GOOGLE_ACCOUNT, your API keys, the five output directories."
echo "  2. ./first_time_login.sh     — signs the bot's Chrome in, inside the recorder"
echo "                                 container, over noVNC (http://127.0.0.1:6080/vnc.html)."
echo "  3. ./pipeline.sh <url-or-file>"
echo "  4. ./webui.sh on             — the web UI (OpenRC) + the resume cron."
echo "  The claude CLI: curl -fsSL https://claude.ai/install.sh | bash, then claude auth login"
echo "  (its musl build), and CLAUDE_CLI_BIN in .env."
