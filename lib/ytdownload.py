#!/usr/bin/env python3
"""Download a YouTube video for frame extraction, retrying transient refusals.

    python3 lib/ytdownload.py URL OUT_TEMPLATE

The one place the yt-dlp download command lives — `run_one.sh`
(`do_fetch_video`) runs this file and `summarize.py` imports it.

Why a retry loop around yt-dlp rather than yt-dlp's own `--retries`: the
failure seen live (2026-10-16, 2 of a 15-video batch) is a 403 on the very
first request for the media, after extraction succeeded. yt-dlp's retries
re-request the same signed googlevideo URL, which stays refused; a fresh
yt-dlp run extracts a fresh URL, and on the resume both videos downloaded in
seconds. So an attempt here is a whole new yt-dlp run. A `.part` left by a
cut-off attempt is resumed by the next (yt-dlp's default `--continue`).

Only transient failures are retried (403, 429, 5xx, timeouts, dropped
connections). A video that is private, removed, region-locked or behind a
bot check fails at once: another attempt would fail identically, and only
delay the report.

    YT_DOWNLOAD_ATTEMPTS        total attempts (default 4)
    YT_DOWNLOAD_RETRY_SECONDS   first wait, doubled each time (default 30)
    YT_DLP_BIN                  the yt-dlp binary (test seam)
"""
import os
import random
import re
import subprocess
import sys
import time

# Muxed first, then *video-only*: the file exists only for frames, so no
# audio is needed and no merge (which needs a JS runtime) ever runs. See
# CLAUDE.md ("The YouTube download never merges streams") before changing it.
FORMAT = ("best[ext=mp4]/best/bv*[ext=mp4][vcodec^=avc1][height<=720]"
          "/bv*[ext=mp4][height<=720]/bv*[height<=720]/bv*")

DEFAULT_ATTEMPTS = 4
DEFAULT_RETRY_SECONDS = 30.0

# Checked first: a match here is never retried, whatever else the output says.
PERMANENT_RE = re.compile(
    r"Video unavailable|Private video|This video is not available"
    r"|has been removed|members[- ]only|Join this channel"
    r"|Sign in to confirm|confirm your age|not available in your country"
    r"|copyright|Requested format is not available|Unsupported URL"
    r"|is not a valid URL|Incomplete YouTube ID",
    re.IGNORECASE)

TRANSIENT_RE = re.compile(
    r"HTTP Error (403|408|429|5\d\d)|timed out|Connection reset"
    r"|Connection aborted|Connection refused|Remote end closed"
    r"|IncompleteRead|Temporary failure in name resolution"
    r"|Name or service not known|Network is unreachable|did not get any data"
    r"|giving up after \d+ (fragment )?retries",
    re.IGNORECASE)

# yt-dlp could not run its JS challenge solver (deno). Without the solved
# challenge YouTube hides the formats and the very same run ends in "This video
# is not available" -- so this wins over PERMANENT_RE. Found 2026-10-16: pm2's
# NODE_CHANNEL_FD leaked into deno and 37 of 193 downloads were given up on.
CHALLENGE_RE = re.compile(
    r"n challenge solving failed|Error solving n challenge"
    r"|Failed to open IPC channel", re.IGNORECASE)

# pm2 sets these on the apps it forks; deno reads NODE_CHANNEL_FD as an IPC
# channel that is not its own and dies.
NODE_IPC_VARS = ("NODE_CHANNEL_FD", "NODE_CHANNEL_SERIALIZATION_MODE",
                 "NODE_APP_INSTANCE")

# Seam for the tests.
_sleep = time.sleep


def is_transient(output):
    """True when yt-dlp's stderr names a failure another attempt may not hit."""
    if CHALLENGE_RE.search(output):
        return True
    if PERMANENT_RE.search(output):
        return False
    return bool(TRANSIENT_RE.search(output))


def _int_env(name, default):
    try:
        return max(1, int(os.environ.get(name, "")))
    except ValueError:
        return default


def _float_env(name, default):
    try:
        return max(0.0, float(os.environ.get(name, "")))
    except ValueError:
        return default


def command(url, out_template):
    return [os.environ.get("YT_DLP_BIN") or "yt-dlp", "--no-playlist",
            "-f", FORMAT, "-o", out_template, url]


def _run_once(cmd, quiet):
    """Run yt-dlp once: stdout passes through (or is dropped when `quiet`),
    stderr is echoed line by line and kept for classification."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL if quiet else None,
        stderr=subprocess.PIPE, text=True, errors="replace",
        env={k: v for k, v in os.environ.items() if k not in NODE_IPC_VARS})
    lines = []
    for line in proc.stderr:
        lines.append(line)
        if not quiet:
            sys.stderr.write(line)
            sys.stderr.flush()
    rc = proc.wait()
    return rc, "".join(lines)


def download(url, out_template, quiet=False, log=None):
    """Download `url` to `out_template`. Returns (exit code, last stderr)."""
    log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
    attempts = _int_env("YT_DOWNLOAD_ATTEMPTS", DEFAULT_ATTEMPTS)
    base = _float_env("YT_DOWNLOAD_RETRY_SECONDS", DEFAULT_RETRY_SECONDS)
    cmd = command(url, out_template)
    for attempt in range(1, attempts + 1):
        rc, err = _run_once(cmd, quiet)
        if rc == 0:
            if attempt > 1:
                log(f"[yt-dlp] downloaded on attempt {attempt} of {attempts}")
            return 0, err
        if not is_transient(err):
            return rc, err
        if attempt == attempts:
            log(f"[yt-dlp] still failing after {attempts} attempts; giving up")
            return rc, err
        # Doubling, with up to +25% jitter so a batch that was refused
        # together does not come back together.
        wait = base * (2 ** (attempt - 1))
        wait += random.uniform(0, wait * 0.25)
        reason = TRANSIENT_RE.search(err).group(0)
        log(f"[yt-dlp] {reason} — fresh attempt in {wait:.0f}s "
            f"({attempt + 1} of {attempts})")
        _sleep(wait)
    return 1, ""


def main(argv):
    if len(argv) != 2:
        print("usage: ytdownload.py URL OUT_TEMPLATE", file=sys.stderr)
        return 2
    rc, _ = download(argv[0], argv[1])
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
