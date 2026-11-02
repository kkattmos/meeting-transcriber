#!/bin/bash
# Process helpers that mean the same thing on Linux and under Git Bash on
# native Windows (the `windows` branch). Sourced by run_one.sh, pipeline.sh,
# kill_meeting.sh and web/resume.sh. On Linux every function is exactly the
# command the scripts used before (`$$`, `kill -0`, `setsid nohup`, the
# `ps --ppid` walk) — nothing changes there.
#
# Why it exists: under Git Bash, `$$` is an MSYS pid, which Windows (and
# Python, which reads run.lock and the slot queue) has never heard of. So on
# Windows the pid a script WRITES is the Windows one (/proc/<pid>/winpid), and
# the liveness test and the tree kill go through tasklist / taskkill.
#
#   mb_is_windows                 true under Git Bash / MSYS / Cygwin
#   mb_pid [BASHPID]              the pid to write into a lock (default $$)
#   mb_alive PID                  is that pid running (as written by mb_pid)
#   mb_tree PID                   PID and all its descendants, one per line
#   mb_kill_tree SIG PID          signal the whole tree (Windows: taskkill /T)
#   mb_detach LOG CMD...          run CMD in the background, immune to the
#                                 terminal closing; prints its pid

mb_is_windows() {
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) return 0 ;;
  esac
  return 1
}

mb_pid() {
  local p="${1:-$$}"
  if mb_is_windows && [ -r "/proc/$p/winpid" ]; then
    cat "/proc/$p/winpid"
  else
    printf '%s\n' "$p"
  fi
}

mb_alive() {
  local p="$1"
  [ -n "$p" ] || return 1
  if mb_is_windows; then
    # tasklist prints "INFO: No tasks ..." and exits 0 when nothing matches,
    # so match the pid column itself.
    tasklist //FI "PID eq $p" //NH 2>/dev/null | grep -qw -- "$p"
  else
    kill -0 "$p" 2>/dev/null
  fi
}

mb_tree() {
  local child
  printf '%s\n' "$1"
  if mb_is_windows; then
    # Children of a Windows pid, via the process table (wmic is gone from
    # Windows 11; PowerShell's CIM is always there).
    for child in $(powershell.exe -NoProfile -Command \
        "Get-CimInstance Win32_Process -Filter 'ParentProcessId=$1' | ForEach-Object { \$_.ProcessId }" \
        2>/dev/null | tr -d '\r'); do
      mb_tree "$child"
    done
  else
    for child in $(ps -o pid= --ppid "$1" 2>/dev/null); do
      mb_tree "$child"
    done
  fi
}

mb_kill_tree() {  # SIG PID
  local sig="$1" pid="$2"
  if mb_is_windows; then
    # Windows has no TERM: /T takes the tree, /F is needed for console apps
    # that ignore the close request. KILL and TERM both end up here.
    taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
  else
    # shellcheck disable=SC2046
    kill -"$sig" $(mb_tree "$pid") 2>/dev/null || true
  fi
}

mb_detach() {  # LOG CMD...
  local log="$1"; shift
  if mb_is_windows || ! command -v setsid >/dev/null 2>&1; then
    # No setsid under Git Bash; nohup + disown keeps it alive when the
    # terminal window closes.
    nohup "$@" >> "$log" 2>&1 < /dev/null &
    local pid=$!
    disown "$pid" 2>/dev/null || true
    echo "$pid"
  else
    setsid nohup "$@" >> "$log" 2>&1 < /dev/null &
    echo "$!"
  fi
}
