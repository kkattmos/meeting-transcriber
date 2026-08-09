#!/bin/bash
# Standalone "leave now" for the meeting recording bot.
#
# Use this when a recording was launched in the background (e.g. via
# trigger_server.py -> pipeline.sh) and you can't reach its terminal to press
# Ctrl+\. From the recording terminal itself, Ctrl+\ also works.
#
# How it stops a recording, in order:
#   1. Touches the kill sentinel in every active run directory. capture.py
#      polls for it and clicks "Leave" in the meeting UI, so other participants
#      see the bot go rather than it vanishing mid-call. This is the whole
#      point — do not "optimize" it into a docker kill.
#   2. SIGTERMs the host-side orchestrator + stage scripts.
#   3. Only if a recorder container is still alive after the grace period,
#      forcibly removes it.
#
# Recordings run one container per run, so this can target a single run instead
# of taking down every meeting at once:
#
#   ./kill_meeting.sh                  # every active run
#   ./kill_meeting.sh --run-id <id>    # just that one
#   ./kill_meeting.sh --list           # show what's running, kill nothing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEETING_BOT_ROOT="${MEETING_BOT_ROOT:-/opt/meeting-bot}"
RUNS_DIR="$MEETING_BOT_ROOT/runs"
LEGACY_SENTINEL="/tmp/meeting_bot_kill"
GRACE_SECONDS="${KILL_GRACE_SECONDS:-25}"

TARGET_RUN_ID=""
LIST_ONLY=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --run-id) TARGET_RUN_ID="${2:-}"; shift 2 ;;
    --list)   LIST_ONLY=1; shift ;;
    -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1 (try --help)" >&2; exit 1 ;;
  esac
done

# --- What's actually running? -----------------------------------------------
mapfile -t REC_CONTAINERS < <(
  docker ps --format '{{.Names}}' 2>/dev/null | grep '^meeting-bot-rec-' || true
)

if [ "$LIST_ONLY" -eq 1 ]; then
  echo "Recorder containers:"
  if [ "${#REC_CONTAINERS[@]}" -eq 0 ]; then
    echo "  (none)"
  else
    printf '  %s\n' "${REC_CONTAINERS[@]}"
  fi
  echo ""
  echo "Recent runs:"
  python3 "$SCRIPT_DIR/lib/runstate.py" list --root "$RUNS_DIR" --limit 10 || true
  exit 0
fi

# --- 1) Set the kill sentinels ----------------------------------------------
# A sentinel in a run dir that has no live recording is harmless: pipeline.sh
# clears it at the start of every run.
SENTINELS_SET=0
if [ -n "$TARGET_RUN_ID" ]; then
  if [ ! -d "$RUNS_DIR/$TARGET_RUN_ID" ]; then
    echo "ERROR: no such run: $RUNS_DIR/$TARGET_RUN_ID" >&2
    echo "  List them with: ./kill_meeting.sh --list" >&2
    exit 1
  fi
  echo "==> Setting kill sentinel for run: $TARGET_RUN_ID"
  touch "$RUNS_DIR/$TARGET_RUN_ID/kill"
  SENTINELS_SET=1
else
  if [ -d "$RUNS_DIR" ]; then
    for run_dir in "$RUNS_DIR"/*/; do
      [ -d "$run_dir" ] || continue
      touch "${run_dir}kill"
      SENTINELS_SET=$((SENTINELS_SET + 1))
    done
  fi
  # The /tmp path is still used by a bare `screen/capture.py <url>` run that
  # never went through the pipeline.
  touch "$LEGACY_SENTINEL"
  echo "==> Set kill sentinel in $SENTINELS_SET run dir(s) (+ the legacy /tmp path)"
fi

# --- 2) SIGTERM the host-side processes --------------------------------------
# Only when killing everything: with --run-id we let the sentinel do the work,
# since the host processes aren't labelled per-run.
if [ -z "$TARGET_RUN_ID" ]; then
  ORCH_PIDS=$(pgrep -f "pipeline\.sh|screen/record_screen\.sh" || true)
  STAGE_PIDS=$(pgrep -f "transcribe/transcribe\.sh|summarize/summarize\.py" || true)

  if [ -n "$ORCH_PIDS" ]; then
    echo "==> SIGTERM to orchestrator (PIDs: $ORCH_PIDS)"
    # shellcheck disable=SC2086
    kill -TERM $ORCH_PIDS 2>/dev/null || true
  fi
  if [ -n "$STAGE_PIDS" ]; then
    echo "==> SIGTERM to transcribe/summarize (PIDs: $STAGE_PIDS)"
    # shellcheck disable=SC2086
    kill -TERM $STAGE_PIDS 2>/dev/null || true
  fi
fi

# --- 3) Give the bot time to click Leave, then escalate ----------------------
if [ "${#REC_CONTAINERS[@]}" -eq 0 ]; then
  echo "==> No recorder containers running. Sentinels are set, so anything that"
  echo "    starts in the next few seconds will leave immediately too."
  exit 0
fi

echo "==> Waiting up to ${GRACE_SECONDS}s for the bot to leave the meeting cleanly..."
for _ in $(seq 1 "$GRACE_SECONDS"); do
  still_up=0
  for name in "${REC_CONTAINERS[@]}"; do
    if [ -n "$TARGET_RUN_ID" ] && [ "$name" != "meeting-bot-rec-$TARGET_RUN_ID" ]; then
      continue
    fi
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$name"; then
      still_up=1
    fi
  done
  if [ "$still_up" -eq 0 ]; then
    echo "==> All recorder containers exited cleanly."
    exit 0
  fi
  sleep 1
done

# capture.py polls every POLL_SECONDS (15s), so anything still alive after the
# grace period is genuinely stuck rather than just slow to notice.
for name in "${REC_CONTAINERS[@]}"; do
  if [ -n "$TARGET_RUN_ID" ] && [ "$name" != "meeting-bot-rec-$TARGET_RUN_ID" ]; then
    continue
  fi
  if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$name"; then
    echo "==> $name didn't exit in time — forcing it down."
    docker rm -f "$name" >/dev/null 2>&1 || true
  fi
done

echo "==> Done."
