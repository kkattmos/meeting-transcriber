#!/bin/bash
# The container's main process (under tini): the web UI / trigger server, plus
# the loop that replaced meeting-bot-resume.timer.
set -uo pipefail
cd /app

PYTHON_BIN="${MEETING_BOT_VENV:-/opt/meeting-bot-venv}/bin/python3"

if [ -z "${MEETING_BOT_TOKEN:-}" ]; then
  echo "ERROR: MEETING_BOT_TOKEN is not set. Put it in .env — any long random" >&2
  echo "  string, e.g.:  python3 -c 'import secrets; print(secrets.token_urlsafe(24))'" >&2
  exit 1
fi

mkdir -p "$MEETING_BOT_ROOT"/{runs,state,tmp,logs} "$CHROME_PROFILE_DIR" \
         "$RESOURCE_CACHE_DIR" "$FRAMES_DIR" \
         "$RECORDINGS_DIR" "$TRANSCRIPTS_DIR" "$SUMMARIES_DIR" "$PDF_DIR"

# Nothing from a previous container is alive: every PID it recorded belongs
# to a namespace that no longer exists, and a fresh container hands out small
# PIDs again — so a stale run.lock or queue holder could match an unrelated
# live process here and look held forever. Outside Docker the pipeline tells
# stale from live with kill -0; across a restart that test lies, so clear them.
find "$MEETING_BOT_ROOT/runs" -mindepth 2 -maxdepth 2 \
     \( -name run.lock -o -name record.pid -o -name admitted \) \
     -exec rm -rf {} + 2>/dev/null || true
rm -rf "$MEETING_BOT_ROOT/queue"

if [ -x "${CLAUDE_CLI_BIN:-}" ] && ! "$CLAUDE_CLI_BIN" auth status >/dev/null 2>&1; then
  echo "NOTE: the claude CLI is not signed in; summaries will fall back to Gemini."
  echo "      Sign in once:  docker compose exec -it bot claude auth login"
fi

# Every 15 minutes, 5 after start — what meeting-bot-resume.timer did. It
# finishes runs that paused on the Claude usage window; --resume-all skips a
# run whose reset time hasn't come, and one another process is working on.
(
  sleep "${RESUME_FIRST_DELAY_SECONDS:-300}"
  while true; do
    ./pipeline.sh --resume-all >> "$MEETING_BOT_ROOT/logs/resume.log" 2>&1
    sleep "${RESUME_INTERVAL_SECONDS:-900}"
  done
) &

export MEETING_BOT_BIND="${MEETING_BOT_BIND:-0.0.0.0}"
exec "$PYTHON_BIN" trigger_server.py
