#!/usr/bin/env python3
"""Is there anything to transcribe in this file?

    python3 lib/audiocheck.py <media> [--noise-db -60] [--min-sound-seconds 30]

Prints one line — `sound=<s>s of <duration>s (<pct>%)` — and exits:
  0  there is sound worth transcribing
  3  effectively silent (no audio track, or less sound than the minimum)
  2  the file could not be analysed (left for the transcriber to judge)

Why not the loudest point: a meeting recording whose presentation carried no
audio (found live 2026-09-29) was digital zero for nine minutes, yet peaked
at -14 dB — Meet's own join and leave chimes at the two ends. What matters is
how much of the file has sound in it, so this sums ffmpeg's silencedetect
intervals and compares what is left against a floor: the chimes are a few
seconds, any real speech is minutes.

transcribe.sh runs it before uploading to AssemblyAI, which bills silence and
returns "no usable transcript" — and the resume job would pay again every 15
minutes.
"""
import argparse
import re
import subprocess
import sys

DURATION_RE = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
SILENCE_START_RE = re.compile(r"silence_start: (-?\d+(?:\.\d+)?)")
SILENCE_DUR_RE = re.compile(r"silence_duration: (\d+(?:\.\d+)?)")


def analyse(path, noise_db=-60.0):
    """(duration_s, silent_s, has_audio) — or None if ffmpeg failed."""
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vn",
             "-af", f"silencedetect=noise={noise_db}dB:d=1", "-f", "null", "-"],
            capture_output=True, text=True, timeout=1800)
    except (OSError, subprocess.TimeoutExpired):
        return None
    err = proc.stderr
    m = DURATION_RE.search(err)
    if not m:
        return None
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    has_audio = "Audio:" in err
    if not has_audio:
        return duration, duration, False
    silent = sum(float(x) for x in SILENCE_DUR_RE.findall(err))
    # A silence still open at the end of the file has a start and no duration.
    starts = SILENCE_START_RE.findall(err)
    ends = SILENCE_DUR_RE.findall(err)
    if len(starts) > len(ends):
        silent += max(0.0, duration - float(starts[-1]))
    return duration, min(silent, duration), True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("path")
    ap.add_argument("--noise-db", type=float, default=-60.0)
    ap.add_argument("--min-sound-seconds", type=float, default=30.0)
    args = ap.parse_args(argv)
    result = analyse(args.path, args.noise_db)
    if result is None:
        print("sound=unknown (ffmpeg could not analyse the file)")
        return 2
    duration, silent, has_audio = result
    if not has_audio:
        print(f"sound=0s of {duration:.0f}s (no audio track)")
        return 3
    sound = duration - silent
    pct = 100.0 * sound / duration if duration else 0.0
    print(f"sound={sound:.0f}s of {duration:.0f}s ({pct:.0f}%)")
    # Short files (a 20-second clip) only need to be mostly sound.
    floor = min(args.min_sound_seconds, 0.5 * duration)
    return 0 if sound >= floor else 3


if __name__ == "__main__":
    sys.exit(main())
