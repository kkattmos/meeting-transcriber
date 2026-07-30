#!/bin/bash
# Standalone "leave now" for the meeting recording bot.
#
# Use this when a recording was launched in the background
# (e.g. via trigger_server.py -> pipeline.sh) and you can't reach its
# terminal to press Ctrl+\. From the recording terminal itself, Ctrl+\
# also works.
#
# What it does:
#   1. Touches /tmp/meeting_bot_kill so screen/capture.py's polling loop
#      notices on its next iteration and clicks "Leave" in the meeting UI
#      (so participants see the bot go, rather than it vanishing).
#   2. Sends SIGTERM to the running orchestrator + join driver + (if
#      active) the transcribe/summarize stages. The script names matched
#      here are the ones introduced by the 3-option split:
#        - pipeline.sh               (chain orchestrator)
#        - screen/record_screen.sh   (Option 1 standalone recorder)
#        - screen/capture.py         (the Playwright join driver)
#        - transcribe/transcribe.sh  (Option 2 standalone)
#        - summarize/summarize.py    (Option 3 standalone)
#   3. Waits up to 10s for processes to exit cleanly. Escalates to SIGKILL
#      on the Python join driver only if it's still alive (the recording
#      shell may still be finalizing the MP4 or finishing whisper.cpp, which
#      is fine to let finish).
#
# Usage:
#   sudo ./kill_meeting.sh
#   (sudo needed if the recording was started with sudo -H, which it should be)

set -e

KILL_SENTINEL="/tmp/meeting_bot_kill"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1) Touch the sentinel so the Python loop picks it up on its next poll
#    (POLL_SECONDS = 15s in screen/capture.py, plus 5s for wait_for_admission).
echo "==> Setting kill sentinel: $KILL_SENTINEL"
touch "$KILL_SENTINEL"

# 2) Find the relevant processes. -f matches the full command line so we
#    only hit our own scripts and not, say, the user's other bash sessions.
#    Patterns match the NEW script layout, not the legacy audio-only one.
ORCH_PIDS=$(pgrep -f "pipeline\.sh|screen/record_screen\.sh" || true)
JOIN_PIDS=$(pgrep -f "screen/capture\.py" || true)
TRANSCRIBE_PIDS=$(pgrep -f "transcribe/transcribe\.sh" || true)
SUMMARIZE_PIDS=$(pgrep -f "summarize/summarize\.py" || true)
ALL_OTHER_PIDS="$TRANSCRIBE_PIDS $SUMMARIZE_PIDS"

if [ -z "$ORCH_PIDS$JOIN_PIDS$ALL_OTHER_PIDS" ]; then
  echo "==> No meeting-bot processes found. Sentinel set anyway - any"
  echo "    capture.py that starts within the next ~15s will exit"
  echo "    immediately, so this is also useful to prevent a future run."
  exit 0
fi

if [ -n "$ORCH_PIDS" ]; then
  echo "==> Sending SIGTERM to orchestrator (pipeline.sh / record_screen.sh) - PIDs: $ORCH_PIDS"
  kill -TERM $ORCH_PIDS 2>/dev/null || true
fi

# capture.py doesn't install a signal handler - SIGTERM is delivered to it
# via the parent process group's TERM from the shell trap. But for the
# kill_meeting.sh case (parent is unrelated), send it directly so the Python
# interpreter exits without waiting for the next poll.
if [ -n "$JOIN_PIDS" ]; then
  echo "==> Sending SIGTERM to screen/capture.py (PIDs: $JOIN_PIDS)"
  kill -TERM $JOIN_PIDS 2>/dev/null || true
fi

# Transcribe/summarize stages don't have any special clean-up; SIGTERM is fine
# to interrupt whisper-cli or the LLM API call.
if [ -n "$ALL_OTHER_PIDS" ]; then
  echo "==> Sending SIGTERM to transcribe/summarize - PIDs: $ALL_OTHER_PIDS"
  kill -TERM $ALL_OTHER_PIDS 2>/dev/null || true
fi

# 3) Wait for the orchestrator shell to exit cleanly. We only escalate on
#    the Python join driver - the shell may legitimately be in whisper-cli
#    (transcription) or finalizing the MP4, and we want those to finish on
#    their own.
echo "==> Waiting up to 10s for processes to exit..."
for i in $(seq 1 20); do
  REMAINING_JOIN=$(pgrep -f "screen/capture\.py" || true)
  REMAINING_ORCH=""
  for pid in $ORCH_PIDS; do
    if kill -0 "$pid" 2>/dev/null; then
      REMAINING_ORCH="$REMAINING_ORCH $pid"
    fi
  done
  if [ -z "$REMAINING_JOIN" ] && [ -z "$REMAINING_ORCH" ]; then
    echo "==> All meeting-bot processes exited cleanly."
    exit 0
  fi
  sleep 0.5
done

# Escalate on the Python join driver only. The recording shell is allowed to
# linger if it's still finishing the MP4 or transcription.
if [ -n "$REMAINING_JOIN" ]; then
  echo "==> screen/capture.py didn't exit in time - sending SIGKILL (PIDs: $REMAINING_JOIN)"
  kill -KILL $REMAINING_JOIN 2>/dev/null || true
fi
if [ -n "$REMAINING_ORCH" ]; then
  echo "==> Orchestrator still running (PIDs:$REMAINING_ORCH) - probably"
  echo "    finishing MP4 mux / transcription. Leaving it alone; Ctrl+C in its"
  echo "    terminal (or sudo kill $REMAINING_ORCH) will stop it if needed."
fi

# Clean up the sentinel so a future run doesn't immediately exit.
rm -f "$KILL_SENTINEL"
echo "==> Done."