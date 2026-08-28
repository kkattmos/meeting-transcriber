#!/bin/bash
# Shared helper for launching the Debian "recorder" container from the Alpine
# host. Sourced by first_time_login.sh and screen/record_screen.sh — the two
# scripts that need real Google Chrome, which cannot run on musl.
#
# bash, not ash: setup.sh installs bash, and arrays are the only way to pass
# docker arguments through without word-splitting/globbing a URL or a path.
#
# Provides:
#   recorder_require_docker      fail loudly if docker isn't usable
#   recorder_require_image       fail loudly if the image isn't built
#   recorder_build <docker_dir>  build (or rebuild) the image
#   recorder_run <name> <detach|attach> [docker args...] -- <command...>
#   recorder_tailscale_ip        this host's Tailscale IPv4, or empty
#
# Everything the container needs is bind-mounted, so editing a .py/.sh file in
# the repo takes effect on the next run with no rebuild.

RECORDER_IMAGE="${RECORDER_IMAGE:-meeting-bot-recorder:latest}"
MEETING_BOT_ROOT="${MEETING_BOT_ROOT:-/opt/meeting-bot}"
# The Chrome profile lives on the host under the output root (NOT under a
# user's $HOME) so it survives container restarts and is shared between the
# login script and the recorder. Inside the container it is mounted at
# ~/.meeting-bot/chrome-profile, the path capture.py already expects.
CHROME_PROFILE_DIR="${CHROME_PROFILE_DIR:-$MEETING_BOT_ROOT/chrome-profile}"

recorder_require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    cat >&2 <<EOF
ERROR: docker is not installed.
  The browser stages need real Google Chrome, which has no musl build, so they
  run in a Debian container. Install it with:
    apk add docker docker-cli-compose && rc-update add docker default && service docker start
  (or just re-run ./setup.sh)
EOF
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    cat >&2 <<EOF
ERROR: docker is installed but the daemon isn't reachable.
  Start it with:  service docker start
  In a Proxmox LXC container the daemon also needs nesting=1
  (Options -> Features -> Nesting) and a usable storage driver.
EOF
    return 1
  fi
  return 0
}

recorder_image_exists() {
  docker image inspect "$RECORDER_IMAGE" >/dev/null 2>&1
}

recorder_require_image() {
  if ! recorder_image_exists; then
    cat >&2 <<EOF
ERROR: recorder image '$RECORDER_IMAGE' is not built.
  Build it with:  ./setup.sh --build-recorder
  (or directly:   docker build -t $RECORDER_IMAGE -f docker/Dockerfile.recorder docker/)
EOF
    return 1
  fi
  return 0
}

recorder_build() {
  local docker_dir="$1"
  echo "==> Building recorder image: $RECORDER_IMAGE"
  echo "    (Debian + real google-chrome-stable; a few minutes the first time)"
  docker build -t "$RECORDER_IMAGE" -f "$docker_dir/Dockerfile.recorder" "$docker_dir"
}

# recorder_run <container_name> <detach|attach> [docker args...] -- <cmd...>
#
# Mounts:
#   $REPO_ROOT          -> /app (read-only: code only, never written to)
#   $MEETING_BOT_ROOT   -> same path (recordings, transcripts, runs/, ...)
#   $CHROME_PROFILE_DIR -> /root/.meeting-bot/chrome-profile (persistent login)
#
# --shm-size=1g: Chrome crashes on Docker's default 64MB /dev/shm as soon as a
# page gets non-trivial. --init reaps the zombies Chrome leaves behind.
recorder_run() {
  local name="$1"; shift
  local mode="$1"; shift

  local -a extra=()
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--" ]; then
      shift
      break
    fi
    extra+=("$1")
    shift
  done
  # Whatever is left in "$@" is the command to run inside the container.

  if [ -z "${REPO_ROOT:-}" ]; then
    echo "ERROR: recorder_run needs REPO_ROOT set by the caller." >&2
    return 1
  fi

  mkdir -p "$CHROME_PROFILE_DIR" "$MEETING_BOT_ROOT"

  local -a detach=()
  [ "$mode" = "detach" ] && detach=(-d)

  # Bare `-e VAR` forwards the host's current value and is a no-op when the var
  # is unset, so values source_env.sh loaded from .env reach capture.py intact.
  docker run --rm "${detach[@]}" --init \
    --name "$name" \
    --shm-size=1g \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -e MAX_MEETING_MINUTES \
    -e IDLE_LEAVE_MINUTES \
    -e MEETING_BOT_RUN_DIR \
    -e TZ \
    -v "$REPO_ROOT:/app:ro" \
    -v "$MEETING_BOT_ROOT:$MEETING_BOT_ROOT" \
    -v "$CHROME_PROFILE_DIR:/root/.meeting-bot/chrome-profile" \
    "${extra[@]}" \
    "$RECORDER_IMAGE" \
    "$@"
}

# Best-effort: this host's Tailscale IPv4, empty if Tailscale isn't up. Used to
# print a URL the operator can actually reach, since they can't see the console.
recorder_tailscale_ip() {
  if command -v tailscale >/dev/null 2>&1; then
    tailscale ip -4 2>/dev/null | head -n 1
  fi
}

# Best-effort: this host's primary routable IPv4. Used when the bind mode is
# 0.0.0.0 (a wildcard, not a destination) so we can print a URL the operator
# can actually open. Empty on hosts with no non-loopback IPv4 — the caller
# falls back to `hostname -i` in that case.
recorder_host_ipv4() {
  # `ip -4 route get 1.1.1.1` returns the source address the kernel would use
  # to reach the public internet. That is the canonical "this host's primary
  # routable IPv4" on every modern Linux, including musl/Alpine.
  ip -4 route get 1.1.1.1 2>/dev/null \
    | awk '/src/ {for (i=1; i<=NF; i++) if ($i=="src") {print $(i+1); exit}}'
}
