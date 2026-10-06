#!/bin/bash
# pm2's entry point for meeting-bot-resume (ecosystem.config.js): start
# `pipeline.sh --resume-all` in a session of its own, then exit at once.
#
# pm2's cron_restart restarts the app on schedule whether or not it is still
# running, and kills its process tree to do so. When pm2 ran pipeline.sh
# directly, every resume that took longer than 15 minutes — any chunked
# summary — was killed at the next tick and started again from chunk 1, for
# ever (2026-10-06, a 9-video combine run). Detached, the resume outlives the
# tick; the next tick's --resume-all skips it because its run.lock owner is
# alive. `./webui.sh off` no longer stops a resume already under way either;
# README says how (TERM to the run's process group — kill_meeting.sh only
# stops recordings).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
# shellcheck disable=SC1091
. "$ROOT_DIR/source_env.sh"
LOG_DIR="$MEETING_BOT_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/resume.log"
# One appended log, rotated at 5MB: a tick with nothing to do still writes the
# member list, 96 times a day.
if [ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 5242880 ]; then
  mv -f "$LOG" "$LOG.1"
fi
printf '\n===== %s resume-all =====\n' "$(date '+%F %T')" >>"$LOG"
cd "$ROOT_DIR"
MEETING_BOT_FOREGROUND=1 setsid nohup "$ROOT_DIR/pipeline.sh" --resume-all \
  >>"$LOG" 2>&1 </dev/null &
echo "resume-all started (pid $!), log: $LOG"
