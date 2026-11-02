#!/bin/bash
# The Alpine host's launcher for the Debian "recorder" container (the `main`
# branch). Sourced by screen/record_in_container.sh, first_time_login.sh,
# kill_meeting.sh and recorder.sh — the scripts that need real Google Chrome,
# which has no musl build.
#
#   recorder_require                  docker usable and the image built, or fail loudly
#   recorder_build                    build (or rebuild) the image
#   recorder_run NAME [docker args...] -- CMD...
#                                     run CMD in a fresh container, attached
#   recorder_running NAME             is that container up
#
# Mounts, each at the SAME path inside as outside, so every path in
# state.json, record.pid and the logs means the same thing on both sides:
#   the repo                 -> /app, read-only (code only; edits need no rebuild)
#   MEETING_BOT_ROOT         -> itself (runs/, the sentinels, logs)
#   RECORDINGS_DIR           -> itself (the MP4 / .m4a is written straight there)
#   CHROME_PROFILE_DIR       -> itself (the persistent Google/Zoom sign-in)
#
# The environment the recorder reads (capture.py's timeouts, BOT_GOOGLE_ACCOUNT,
# RECORD_*, MEETING_BOT_RUN_DIR, …) is handed over by name (RECORDER_ENV_RE).

RECORDER_IMAGE="${RECORDER_IMAGE:-meeting-bot-recorder:latest}"
# Everything the browser stage reads; anything else (API keys!) stays on the host.
RECORDER_ENV_RE='^(MEETING_BOT_(ROOT|RUN_DIR|LANGUAGE|KIOSK_MONITOR)|RECORD(INGS)?_[A-Z_]*|RECORD_MEDIA|BOT_GOOGLE_ACCOUNT|CHROME_PROFILE_DIR|FIREFOX_PROFILE_DIR|MAX_MEETING_MINUTES|IDLE_LEAVE_MINUTES|AUTO_LEAVE_[A-Z_]*|NEW_MEET_[A-Z_]*|HOST_ADMIT_[A-Z_]*|ADMIT_[A-Z_]*|AUDIO_SILENCE_[A-Z_]*|KILL_[A-Z_]*|POLL_SECONDS|DISPLAY_MIN|DISPLAY_MAX|TZ)='

recorder_require() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed. The browser stage needs real Google Chrome," >&2
    echo "  which has no musl build, so it runs in a Debian container. ./setup.sh installs it." >&2
    return 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "ERROR: the docker daemon isn't reachable. Start it with:  rc-service docker start" >&2
    echo "  (In a Proxmox LXC container the daemon also needs nesting=1.)" >&2
    return 1
  fi
  if ! docker image inspect "$RECORDER_IMAGE" >/dev/null 2>&1; then
    echo "ERROR: the recorder image '$RECORDER_IMAGE' is not built. Build it with:" >&2
    echo "    ./setup.sh --build-recorder" >&2
    return 1
  fi
}

recorder_build() {
  local repo="${REPO_ROOT:?REPO_ROOT not set}"
  echo "==> Building the recorder image $RECORDER_IMAGE (Debian + real Chrome)"
  docker build -t "$RECORDER_IMAGE" -f "$repo/docker/Dockerfile.recorder" "$repo"
}

recorder_running() {
  [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ]
}

recorder_run() {
  local name="$1"; shift
  local -a extra=()
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do extra+=("$1"); shift; done
  [ "${1:-}" = "--" ] && shift
  local repo="${REPO_ROOT:?REPO_ROOT not set}"
  local envfile
  envfile="$(mktemp)"
  env | grep -E "$RECORDER_ENV_RE" > "$envfile" || true
  local -a mounts=(-v "$repo:/app:ro")
  local dir
  for dir in "${MEETING_BOT_ROOT:-}" "${RECORDINGS_DIR:-}" "${CHROME_PROFILE_DIR:-}"; do
    [ -n "$dir" ] || continue
    mkdir -p "$dir"
    mounts+=(-v "$dir:$dir")
  done
  # --shm-size: Chrome crashes on Docker's 64MB /dev/shm. --init reaps the
  # zombies Chrome leaves (tini is also the image's entrypoint).
  local rc=0
  docker run --rm --init --name "$name" --shm-size=1g \
    --env-file "$envfile" -e MEETING_BOT_IN_RECORDER=1 \
    "${mounts[@]}" "${extra[@]}" "$RECORDER_IMAGE" "$@" || rc=$?
  rm -f "$envfile"
  return "$rc"
}
