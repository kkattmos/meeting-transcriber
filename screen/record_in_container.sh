#!/bin/bash
# The Alpine host's recorder (MEETING_BOT_RECORDER in .env.example): the same
# arguments and contract as screen/record_screen.sh, which it runs inside the
# Debian recorder container (docker/recorder_lib.sh). Chrome has no musl
# build; everything else about recording is the Linux recorder, unchanged.
#
# The container's name goes into runs/<id>/record.container, so
# kill_meeting.sh can act inside the container's PID namespace: record.pid,
# written by record_screen.sh in there, holds the container's pids, which
# mean nothing (or something else!) on the host.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
export REPO_ROOT
# shellcheck disable=SC1091
. "$REPO_ROOT/source_env.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/docker/recorder_lib.sh"
recorder_require

RUN_DIR="${MEETING_BOT_RUN_DIR:-}"
NAME="mb-rec-$(basename "${RUN_DIR:-standalone}")-$$"
NAME="$(printf '%s' "$NAME" | tr -c 'A-Za-z0-9_.-' '_' | cut -c1-120)"
if [ -n "$RUN_DIR" ]; then
  echo "$NAME" > "$RUN_DIR/record.container"
  trap 'rm -f "$RUN_DIR/record.container"' EXIT
fi
# docker run in the foreground forwards INT/TERM to the container, where
# record_screen.sh turns them into "leave the meeting".
recorder_run "$NAME" -- bash /app/screen/record_screen.sh "$@"
