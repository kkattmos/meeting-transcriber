#!/bin/bash
# One-time setup for the meeting recording + transcription bot.
# Bash (not ash): the recorder-build step sources docker/recorder_lib.sh, which
# uses bash arrays. setup.sh's own logic stays POSIX-friendly.
# Target: Alpine Linux on Proxmox (LXC container or KVM VM), 4 vCPU / 8GB RAM.
#
# Split of responsibilities:
#   * Alpine host  — Python, ffmpeg, yt-dlp, the transcribe + summarize stages,
#                    and the docker daemon.
#   * Debian container (docker/Dockerfile.recorder) — Xvfb, PulseAudio, real
#                    Google Chrome, Playwright: everything Stage 1 needs.
#     Chrome has no musl build and Playwright doesn't support Alpine, so the
#     browser half cannot run natively here. See CLAUDE.md.
#
# Idempotent: re-running is safe and cheap. Flags:
#   --build-recorder   only (re)build the container image, skip everything else
#   --no-recorder      skip the container image build (host-side setup only)
#   --no-docker        don't install/enable docker at all (stages 2+3 only box)
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_RECORDER=1
INSTALL_DOCKER=1
HOST_SETUP=1

for arg in "$@"; do
  case "$arg" in
    --build-recorder) HOST_SETUP=0 ;;
    --no-recorder)    BUILD_RECORDER=0 ;;
    --no-docker)      INSTALL_DOCKER=0; BUILD_RECORDER=0 ;;
    -h|--help)
      sed -n '2,20p' "$0"
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

if ! command -v apk >/dev/null 2>&1; then
  echo "ERROR: apk not found. This setup script targets Alpine Linux." >&2
  echo "  The Ubuntu/Debian variant was dropped when the project moved to Alpine;" >&2
  echo "  see CLAUDE.md for the rationale and 'git log -- setup.sh' for the old one." >&2
  exit 1
fi

VENV="/opt/meeting-bot-venv"
BOT_ROOT="/opt/meeting-bot"

if [ "$HOST_SETUP" -eq 1 ]; then

  echo "==> Enabling the community repository (docker, deno and friends live there)"
  # Alpine ships main enabled and community commented out on some images.
  if ! grep -qE '^[^#]*/community' /etc/apk/repositories 2>/dev/null; then
    sed -i 's|^#\(.*/community\)|\1|' /etc/apk/repositories || true
  fi

  echo "==> Updating package index"
  apk update

  echo "==> Installing base tools"
  # bash          — the entry scripts are bash; Alpine's default shell is ash
  # ffmpeg        — frame extraction (stage 3) and audio demux (stage 2)
  # procps/psmisc — real pkill/fuser; busybox's applets differ in flag handling
  # tzdata        — timestamps in run state and output filenames
  apk add --no-cache \
    bash ca-certificates curl wget git \
    ffmpeg \
    python3 py3-pip \
    procps psmisc \
    tzdata

  echo "==> Setting up the Python venv"
  # --system-site-packages is deliberately NOT used: we want a self-contained
  # venv so an apk upgrade of py3-* can't silently change SDK versions.
  if [ ! -x "$VENV/bin/python3" ]; then
    python3 -m venv "$VENV"
  fi
  "$VENV/bin/pip" install --upgrade pip

  # anthropic:    Anthropic API SDK (also used against the FCC proxy).
  # openai:       NVIDIA NIM and any other OpenAI-compatible endpoint.
  # google-genai: Gemini summaries.
  # requests:     youtube-transcript.io client (urllib gets rate-limited where
  #               requests doesn't — different User-Agent and TLS stack).
  # assemblyai:   pre-recorded transcription API for local files.
  #
  # NOTE: playwright is NOT installed here. The join driver runs inside the
  # recorder container, which has its own venv with playwright in it.
  PY_DEPS="anthropic openai google-genai requests assemblyai"
  echo "==> Installing Python dependencies"
  if ! "$VENV/bin/pip" install --no-cache-dir $PY_DEPS; then
    # Most of these ship musllinux wheels, but a transitive dep (typically
    # pydantic-core, which is Rust) may need to be built from source on a
    # newer Alpine than the wheel set covers. Pull the toolchain and retry
    # rather than failing setup outright.
    echo "==> pip failed with wheels only; installing a build toolchain and retrying"
    apk add --no-cache build-base python3-dev libffi-dev openssl-dev cargo rust
    "$VENV/bin/pip" install --no-cache-dir $PY_DEPS
  fi

  echo "==> Installing latest yt-dlp"
  # From GitHub releases, not apk: YouTube breaks yt-dlp regularly and a stale
  # binary is the #1 cause of silent failures. The release artifact is a
  # Python zipapp, so it runs fine on musl as long as python3 is present.
  curl -fL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp
  chmod a+rx /usr/local/bin/yt-dlp
  # A firewall that returns an HTML error page instead of the binary fails here
  # rather than three stages into a real run.
  /usr/local/bin/yt-dlp --version

  echo "==> Installing deno (optional JS runtime for yt-dlp)"
  # Only needed if YouTube ever forces us back onto a format that requires
  # signature computation. The download path we use ("best[ext=mp4]/best")
  # does not need it, so a missing deno is a warning, not an error.
  # Note: deno's official release tarballs are glibc-linked and will NOT run
  # on Alpine — the apk package is the musl build and the only option here.
  if ! command -v deno >/dev/null 2>&1; then
    apk add --no-cache deno 2>/dev/null || \
      echo "    deno not available in your repositories — skipping (optional)."
  fi
  command -v deno >/dev/null 2>&1 && deno --version | head -n 1

  echo "==> Creating working directories"
  # runs/  — per-run state, logs and sentinels (resume + concurrency)
  # tmp/   — YouTube downloads; deliberately NOT /tmp, which is a small tmpfs
  mkdir -p "$BOT_ROOT"/recordings \
           "$BOT_ROOT"/transcripts \
           "$BOT_ROOT"/summaries \
           "$BOT_ROOT"/frames \
           "$BOT_ROOT"/runs \
           "$BOT_ROOT"/tmp \
           "$BOT_ROOT"/secrets \
           "$BOT_ROOT"/chrome-profile

  # Starter keys file for the youtube-transcript.io client. The user fills in
  # the list; the client rotates through it round-robin.
  KEYS_FILE="$BOT_ROOT/secrets/youtube_transcript_keys.json"
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

  if [ "$INSTALL_DOCKER" -eq 1 ]; then
    echo "==> Installing docker (runs the Chrome/recorder container)"
    apk add --no-cache docker docker-cli-compose
    if command -v rc-update >/dev/null 2>&1; then
      rc-update add docker default 2>/dev/null || true
      # `service docker start` is a no-op if it's already running.
      service docker start 2>/dev/null || rc-service docker start 2>/dev/null || true
    fi
    # Give the daemon a moment to open its socket before we probe it.
    i=0
    while [ "$i" -lt 15 ]; do
      docker info >/dev/null 2>&1 && break
      i=$((i + 1))
      sleep 1
    done
    if ! docker info >/dev/null 2>&1; then
      echo "    WARNING: the docker daemon isn't answering yet."
      echo "    In a Proxmox LXC container it also needs nesting=1"
      echo "    (Options -> Features -> Nesting) and a usable storage driver."
      echo "    Start it by hand with:  service docker start"
      BUILD_RECORDER=0
    fi
  fi
fi

if [ "$BUILD_RECORDER" -eq 1 ]; then
  if docker info >/dev/null 2>&1; then
    # shellcheck disable=SC1091
    . "$SCRIPT_DIR/docker/recorder_lib.sh"
    recorder_build "$SCRIPT_DIR/docker"
  else
    echo "==> Skipping recorder image build (docker daemon unreachable)."
    echo "    Once it's up:  ./setup.sh --build-recorder"
  fi
fi

echo ""
echo "==> Done."
echo "Next steps:"
echo "  1. cp .env.example .env && chmod 600 .env  — then fill in your API keys."
echo "  2. Add at least one youtube-transcript.io token to"
echo "     $BOT_ROOT/secrets/youtube_transcript_keys.json (YouTube inputs only)."
echo "  3. ./first_time_login.sh   — sign into Google/Zoom in the persistent"
echo "     Chrome profile. It prints an SSH-tunnel command you run from your"
echo "     own machine, since the VM has no visible display."
