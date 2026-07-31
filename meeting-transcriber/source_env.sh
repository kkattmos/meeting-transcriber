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

ENV_FILE="$(_find_env || true)"
# No .env found: silently return so callers in set -e scripts don't blow up.
# `return` works here even when this script is sourced because the
# `return` builtin is permitted at the top level of a sourced file.
if [ -z "$ENV_FILE" ]; then
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

  # Already exported? Skip — caller override wins.
  # Use `printenv` so we don't false-positive on a same-name local var.
  if printenv "$key" >/dev/null 2>&1; then
    continue
  fi
  export "$key=$val"
done < "$ENV_FILE"