#!/bin/bash
# Loads `.env` from the repo root into the current shell.
#
# Used by every entry script (transcribe.sh, summarize.py, pipeline.sh) so
# the same env-var surface covers every stage. The format is the standard
# "key=value per line, # for comments, blank lines ignored" that
# docker / systemd / dotenv all support. Quoted values have their quotes
# stripped.
#
# Behaviour:
#   - Looks for .env first in the directory of the calling script (so
#     pipeline.sh etc. can live elsewhere), then in the working directory,
#     then in the repo root relative to this loader file.
#   - Does NOT override a value that's already exported — that lets you
#     `KEY=val ./pipeline.sh ...` for one-off overrides without editing
#     the file.
#   - Except with MEETING_BOT_ENV_FRESH=1 (pm2's entry scripts, web/serve.sh
#     and web/resume.sh): then .env wins. Under pm2 an already-set .env key
#     is never an override — it is a copy taken when the daemon or the app
#     started, and honouring it ran a 2026-10-14 summary on the Gemini chain
#     .env had dropped six minutes earlier. The flag is unset after loading.
#   - Exits 0 silently if no .env exists (env vars simply come from the
#     calling shell instead).
#
# Usage:
#   source ./source_env.sh            # in another bash script
#   or
#   . "$(dirname "$0")/source_env.sh" # relative to caller

# Resolve the directory of THIS file (source_env.sh), not the caller.
# BASH_SOURCE[0] is set when sourced; $0 may be the caller or "bash".
if [ -n "${BASH_SOURCE[0]:-}" ]; then
  LOADER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
  LOADER_DIR="$(cd "$(dirname "$0")" && pwd)"
fi

# Search order: caller dir -> cwd -> loader dir (repo root).
_find_env() {
  for candidate in \
      "${SCRIPT_DIR:-}/.env" \
      "./.env" \
      "$LOADER_DIR/.env"; do
    [ -z "$candidate" ] && continue
    if [ -f "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

# Per-user defaults, applied AFTER .env so a value there (or an exported one)
# always wins. The bot runs as the desktop user, not root: its bookkeeping
# lives under XDG data, and the venv is the uv-built `.venv` in the repo, so
# `rm -rf .venv` removes every Python dependency in one go.
_meeting_bot_defaults() {
  : "${MEETING_BOT_ROOT:=${XDG_DATA_HOME:-$HOME/.local/share}/meeting-bot}"
  : "${MEETING_BOT_VENV:=$LOADER_DIR/.venv}"
  export MEETING_BOT_ROOT MEETING_BOT_VENV
  _meeting_bot_windows_paths
}

# Under Git Bash on Windows (the `windows` branch), a path written `~/…` or
# taken from $HOME is `/c/Users/…`: bash understands it, but the Windows
# Python and ffmpeg the scripts hand it to through the ENVIRONMENT do not
# (MSYS converts command-line arguments, never variables). So every
# path-like setting is rewritten to the mixed form `C:/Users/…`, which both
# understand. A no-op everywhere else.
_meeting_bot_windows_paths() {
  case "$(uname -s 2>/dev/null)" in MINGW*|MSYS*|CYGWIN*) ;; *) return 0 ;; esac
  command -v cygpath >/dev/null 2>&1 || return 0
  local name val
  while IFS= read -r name; do
    val="${!name}"
    case "$val" in
      /*) export "$name=$(cygpath -m "$val")" ;;
    esac
  done < <(compgen -e | grep -E '(_DIR|_ROOT|_VENV|_BIN|_FILE|_DIRECTORIES|_RECORDER)$')
  # A bare `python3` in Git Bash is often the Microsoft Store stub; the
  # venv's bin/python3 (a shim setup.ps1 writes) is the real one.
  if [ -n "${MEETING_BOT_VENV:-}" ] && [ -d "$MEETING_BOT_VENV/bin" ]; then
    case ":$PATH:" in
      *":$(cygpath -u "$MEETING_BOT_VENV")/bin:"*) ;;
      *) export PATH="$(cygpath -u "$MEETING_BOT_VENV")/bin:$PATH" ;;
    esac
  fi
}

ENV_FILE="$(_find_env || true)"
# No .env found: silently return so callers in set -e scripts don't blow up.
# `return` works here even when this script is sourced because the
# `return` builtin is permitted at the top level of a sourced file.
if [ -z "$ENV_FILE" ]; then
  _meeting_bot_defaults
  return 0 2>/dev/null || true
  # Fallback for the rare case where return is somehow blocked
  # (e.g. POSIX sh that has no `return` outside functions). The caller
  # will simply continue without env vars loaded — same end state.
fi

# Strip a single layer of surrounding " or ' from a value.
_strip_quotes() {
  local v="$1"
  if [ "${v:0:1}" = '"' ] && [ "${v: -1}" = '"' ]; then
    v="${v:1:-1}"
  elif [ "${v:0:1}" = "'" ] && [ "${v: -1}" = "'" ]; then
    v="${v:1:-1}"
  fi
  printf '%s' "$v"
}

# Read the file once and only export keys that aren't already set.
while IFS= read -r line || [ -n "$line" ]; do
  # Trim leading whitespace.
  line="${line#"${line%%[![:space:]]*}"}"
  # Skip blanks and comments.
  [ -z "$line" ] && continue
  case "$line" in \#*) continue ;; esac
  # Need an `=`; skip lines without one (defensive).
  case "$line" in *=*) ;; *) continue ;; esac
  key="${line%%=*}"
  val="${line#*=}"
  # Strip leading whitespace from key, trailing CR from val.
  key="$(printf '%s' "$key" | tr -d '[:space:]')"
  val="${val%$'\r'}"
  val="$(_strip_quotes "$val")"
  # "~/..." means the home directory — the bot runs as the desktop user, so
  # .env.example can ship per-user paths without naming anyone's $HOME.
  case "$val" in "~/"*) val="$HOME/${val#\~/}" ;; esac

  # Already exported? Skip — caller override wins.
  # Use `printenv` so we don't false-positive on a same-name local var.
  if [ -z "${MEETING_BOT_ENV_FRESH:-}" ] && printenv "$key" >/dev/null 2>&1; then
    continue
  fi
  export "$key=$val"
done < "$ENV_FILE"
unset MEETING_BOT_ENV_FRESH
_meeting_bot_defaults
