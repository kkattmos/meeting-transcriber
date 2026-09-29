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
#      point — do not "optimize" it into a SIGKILL.
#   2. SIGTERMs the host-side orchestrator + stage scripts.
#   3. Only if a recorder is still alive after the grace period, stops that
#      run's browser driver (from $RUN_DIR/record.pid) and waits up to
#      KILL_FINALISE_SECONDS (120) for record_screen.sh to finalise the MP4.
#      ffmpeg is never SIGKILLed, and never gets a second SIGINT — see the
#      comment at the escalation below.
#
# Each recording writes its own pid file, so this can target a single run
# instead of taking down every meeting at once:
#
#   ./kill_meeting.sh                  # every active run
#   ./kill_meeting.sh --run-id <id>    # just that one
#   ./kill_meeting.sh --list           # show what's running, kill nothing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$SCRIPT_DIR/source_env.sh"

MEETING_BOT_ROOT="${MEETING_BOT_ROOT:-$HOME/.local/share/meeting-bot}"
RUNS_DIR="$MEETING_BOT_ROOT/runs"
LEGACY_SENTINEL="/tmp/meeting_bot_kill"
GRACE_SECONDS="${KILL_GRACE_SECONDS:-25}"
FINALISE_SECONDS="${KILL_FINALISE_SECONDS:-120}"

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
# A run is "recording" if its record.pid names a live process. The pid file is
# written by screen/record_screen.sh and removed by its EXIT trap, so a stale
# one only survives a SIGKILL — and the liveness check covers that.
read_pid_field() {
  local file="$1" field="$2"
  [ -f "$file" ] || return 1
  awk -F= -v k="$field" '$1==k {print $2; exit}' "$file"
}

declare -a ACTIVE_RUNS=()
if [ -d "$RUNS_DIR" ]; then
  for run_dir in "$RUNS_DIR"/*/; do
    [ -d "$run_dir" ] || continue
    pid_file="${run_dir}record.pid"
    [ -f "$pid_file" ] || continue
    rec_pid="$(read_pid_field "$pid_file" record || true)"
    if [ -n "$rec_pid" ] && kill -0 "$rec_pid" 2>/dev/null; then
      ACTIVE_RUNS+=("${run_dir%/}")
    fi
  done
fi

if [ "$LIST_ONLY" -eq 1 ]; then
  echo "Active recordings:"
  if [ "${#ACTIVE_RUNS[@]}" -eq 0 ]; then
    echo "  (none)"
  else
    for run_dir in "${ACTIVE_RUNS[@]}"; do
      pid_file="$run_dir/record.pid"
      echo "  $(basename "$run_dir")  (pid $(read_pid_field "$pid_file" record), \
display :$(read_pid_field "$pid_file" display || echo '?'))"
    done
  fi
  echo ""
  echo "Recent runs:"
  python3 "$SCRIPT_DIR/lib/runstate.py" list --root "$RUNS_DIR" --limit 10 || true
  exit 0
fi

# --- 1) Set the kill sentinels ----------------------------------------------
# A sentinel in a run dir that has no live recording is harmless: run_one.sh
# clears it at the start of every run.
if [ -n "$TARGET_RUN_ID" ]; then
  if [ ! -d "$RUNS_DIR/$TARGET_RUN_ID" ]; then
    echo "ERROR: no such run: $RUNS_DIR/$TARGET_RUN_ID" >&2
    echo "  List them with: ./kill_meeting.sh --list" >&2
    exit 1
  fi
  echo "==> Setting kill sentinel for run: $TARGET_RUN_ID"
  touch "$RUNS_DIR/$TARGET_RUN_ID/kill"
else
  SENTINELS_SET=0
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
# Only when killing everything: with --run-id we let the sentinel do the work
# and escalate through that run's own pid file below.
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
declare -a TARGETS=()
for run_dir in "${ACTIVE_RUNS[@]:-}"; do
  [ -n "$run_dir" ] || continue
  if [ -n "$TARGET_RUN_ID" ] && [ "$(basename "$run_dir")" != "$TARGET_RUN_ID" ]; then
    continue
  fi
  TARGETS+=("$run_dir")
done

if [ "${#TARGETS[@]}" -eq 0 ]; then
  echo "==> No recordings are running. Sentinels are set, so anything that"
  echo "    starts in the next few seconds will leave immediately too."
  exit 0
fi

echo "==> Waiting up to ${GRACE_SECONDS}s for the bot to leave the meeting cleanly..."
for _ in $(seq 1 "$GRACE_SECONDS"); do
  still_up=0
  for run_dir in "${TARGETS[@]}"; do
    rec_pid="$(read_pid_field "$run_dir/record.pid" record || true)"
    if [ -n "$rec_pid" ] && kill -0 "$rec_pid" 2>/dev/null; then
      still_up=1
    fi
  done
  if [ "$still_up" -eq 0 ]; then
    echo "==> All recordings exited cleanly."
    exit 0
  fi
  sleep 1
done

# capture.py polls every POLL_SECONDS (15s), so anything still alive after the
# grace period is genuinely stuck rather than just slow to notice.
for run_dir in "${TARGETS[@]}"; do
  pid_file="$run_dir/record.pid"
  run_id="$(basename "$run_dir")"
  rec_pid="$(read_pid_field "$pid_file" record || true)"
  [ -n "$rec_pid" ] && kill -0 "$rec_pid" 2>/dev/null || continue
  echo "==> $run_id didn't exit in time — forcing it down."
  # The browser driver only. record_screen.sh notices it has gone, stops
  # ffmpeg itself with exactly ONE SIGINT and waits for the file to be
  # finalised. This script used to SIGINT ffmpeg as well: two signals while
  # ffmpeg closed the file made it abandon the final write ("Error closing
  # file: Immediate exit requested") and two recordings were unplayable on
  # 2026-09-29. ffmpeg is signalled from here only if its parent is gone.
  join_pid="$(read_pid_field "$pid_file" join || true)"
  ff_pid="$(read_pid_field "$pid_file" ffmpeg || true)"
  # TERM lets capture.py quit its browser (geckodriver + Firefox); a KILL
  # straight away orphaned both, still holding the profile.
  [ -n "$join_pid" ] && kill -TERM "$join_pid" 2>/dev/null || true
  for _ in $(seq 1 15); do
    [ -n "$join_pid" ] && kill -0 "$join_pid" 2>/dev/null || break
    sleep 1
  done
  [ -n "$join_pid" ] && kill -0 "$join_pid" 2>/dev/null && kill -KILL "$join_pid" 2>/dev/null || true
  # Finalising writes the index at the end of the file; on a network drive
  # that takes a while. Never SIGKILL ffmpeg.
  echo "==> Waiting up to ${FINALISE_SECONDS}s for the recording to be finalised..."
  for _ in $(seq 1 "$FINALISE_SECONDS"); do
    rec_alive=0; ff_alive=0
    kill -0 "$rec_pid" 2>/dev/null && rec_alive=1
    [ -n "$ff_pid" ] && kill -0 "$ff_pid" 2>/dev/null && ff_alive=1
    if [ "$rec_alive" -eq 0 ] && [ "$ff_alive" -eq 1 ] && [ -z "${ff_signalled:-}" ]; then
      kill -INT "$ff_pid" 2>/dev/null || true       # orphaned: nobody else will
      ff_signalled=1
    fi
    [ "$rec_alive" -eq 0 ] && [ "$ff_alive" -eq 0 ] && break
    sleep 1
  done
  if kill -0 "$rec_pid" 2>/dev/null || { [ -n "$ff_pid" ] && kill -0 "$ff_pid" 2>/dev/null; }; then
    echo "WARNING: $run_id is still finalising its recording (pids $rec_pid ${ff_pid:-}) —"
    echo "         left alone; it will finish on its own."
  fi
  unset ff_signalled
done

echo "==> Done."
