#!/bin/sh
# Shared helper for launching the Debian "recorder" container from the Alpine
# host. Sourced by first_time_login.sh and screen/record_screen.sh — the two
# scripts that need real Google Chrome, which cannot run on musl.
#
# POSIX sh (not bash): Alpine's default shell is ash, and the host side of
# this project no longer assumes bash is installed.
#
# Provides:
#   recorder_require_docker      fail loudly if docker isn't usable
#   recorder_require_image       fail loudly if the image isn't built
#   recorder_build               build (or rebuild) the image
#   recorder_run <name> <mode> [extra docker args...] -- <command...>
#
# Everything the container needs is bind-mounted, so editing a .py/.sh file in
# the repo takes effect on the next run with no rebuild.

RECORDER_IMAGE="${RECORDER_IMAGE:-meeting-bot-recorder:latest}"
MEETING_BOT_ROOT="${MEETING_BOT_ROOT:-/opt/meeting-bot}"
# The Chrome profile lives on the host under the output root (NOT under a
# user's $HOME) so it survives container restarts and is shared between the
# login script and the recorder. Inside the container it is mounted at
# ~/.meeting-bot/chrome-profile, which is the path capture.py already expects.
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-$MEETING_BOT_ROOT/chrome-profile}"

recorder_docker_bin() {
  if command -v docker >/dev/null 2>&1; then
    echo docker
    return 0
  fi
  return 1
}

recorder_require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed." >&2
    echo "  The browser stages need real Google Chrome, which has no musl build," >&2
    echo "  so they run in a Debian container. Install it with:" >&2
    echo "    apk add docker docker-cli-compose && rc-update add docker default && service docker start" >&2
    echo "  (or just re-run ./setup.sh)" >&2
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "ERROR: docker is installed but the daemon isn't reachable." >&2
    echo "  Start it with:  service docker start" >&2
    echo "  In an LXC container, the daemon also needs nesting=1 on the CT config" >&2
    echo "  (Proxmox: Options -> Features -> Nesting) and a working overlay/fuse driver." >&2
    return 1
  fi
  return 0
}

recorder_image_exists() {
  docker image inspect "$RECORDER_IMAGE" >/dev/null 2>&1
}

recorder_require_image() {
  if ! recorder_image_exists; then
    echo "ERROR: recorder image '$RECORDER_IMAGE' is not built." >&2
    echo "  Build it with:  ./setup.sh --build-recorder" >&2
    echo "  (or directly:   docker build -t $RECORDER_IMAGE -f docker/Dockerfile.recorder docker/)" >&2
    return 1
  fi
  return 0
}

recorder_build() {
  _lib_dir="$1"
  echo "==> Building recorder image: $RECORDER_IMAGE"
  echo "    (Debian + real google-chrome-stable; this takes a few minutes the first time)"
  docker build -t "$RECORDER_IMAGE" -f "$_lib_dir/Dockerfile.recorder" "$_lib_dir"
}

# recorder_run <container_name> <detach|attach> [extra docker args...] -- <cmd...>
#
# Mounts:
#   $REPO_ROOT              -> /app  (read-only; code only, never written to)
#   $MEETING_BOT_ROOT       -> same path (recordings, transcripts, runs/, ...)
#   $CHROME_PROFILE_DIR     -> /root/.meeting-bot/chrome-profile (persistent login)
#
# --shm-size=1g: Chrome crashes on Docker's default 64MB /dev/shm as soon as a
# page gets non-trivial. --init reaps the zombie processes Chrome leaves behind.
recorder_run() {
  _name="$1"; shift
  _mode="$1"; shift

  _extra=""
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--" ]; then
      shift
      break
    fi
    _extra="$_extra $1"
    shift
  done

  if [ -z "${REPO_ROOT:-}" ]; then
    echo "ERROR: recorder_run needs REPO_ROOT set by the caller." >&2
    return 1
  fi

  mkdir -p "$CHROME_PROFILE_DIR" "$MEETING_BOT_ROOT"

  _detach=""
  if [ "$_mode" = "detach" ]; then
    _detach="-d"
  fi

  # Forward the tuning knobs the join driver reads. Bare `-e VAR` passes the
  # host's current value through (and is a no-op when the var is unset), so
  # values loaded from .env by source_env.sh reach capture.py unchanged.
  # shellcheck disable=SC2086
  docker run --rm $_detach --init \
    --name "$_name" \
    --shm-size=1g \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e MAX_MEETING_MINUTES \
    -e IDLE_LEAVE_MINUTES \
    -e MEETING_BOT_RUN_DIR \
    -e TZ \
    -v "$REPO_ROOT:/app:ro" \
    -v "$MEETING_BOT_ROOT:$MEETING_BOT_ROOT" \
    -v "$CHROME_PROFILE_DIR:/root/.meeting-bot/chrome-profile" \
    $_extra \
    "$RECORDER_IMAGE" \
    "$@"
}

# Best-effort: the Tailscale IPv4 of this host, empty if Tailscale isn't up.
# Used to tell the operator a URL they can actually reach, since they can't
# see the VM's console.
recorder_tailscale_ip() {
  if command -v tailscale >/dev/null 2>&1; then
    tailscale ip -4 2>/dev/null | head -n 1
  fi
}
