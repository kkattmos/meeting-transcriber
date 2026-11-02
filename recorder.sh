#!/bin/bash
# Run any of the repo's browser-side commands inside the Debian recorder
# container (the `main` / Alpine branch), with the same mounts as a recording:
#
#   ./recorder.sh ./verify_e2e.sh --browser-smoke
#   ./recorder.sh ./benchmark.sh --browser
#   ./recorder.sh bash                       # a shell in there
#   ./recorder.sh --build                    # (re)build the image
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export REPO_ROOT
# shellcheck disable=SC1091
. "$REPO_ROOT/source_env.sh"
# shellcheck disable=SC1091
. "$REPO_ROOT/docker/recorder_lib.sh"
if [ "${1:-}" = "--build" ]; then recorder_build; exit $?; fi
recorder_require
[ "$#" -gt 0 ] || set -- bash
recorder_run "mb-exec-$$" -it -w /app -- "$@"
