#!/usr/bin/env python3
"""
Parse and apply a --clip time window: "summarize 00:05:00 to 01:30:00 of this".

Two halves, both of them used from bash:

  parse  — turn the operator's spec into seconds, a label, and a run-id token.
           Offline, dependency-free, and it never touches ffmpeg, because
           pipeline.sh runs it while it is still classifying arguments.
  cut    — trim a media file to the window with ffmpeg.

The window is applied by CUTTING THE MEDIA, not by filtering downstream: the
clip is transcribed and frame-extracted as if it were the whole video, so
AssemblyAI only bills the minutes asked for and no stage after this one needs
to know a window existed. The consequence is that every timestamp in the
output — SRT cues, "Frame 4 @ 0:02:11", the hidden transcript — is
CLIP-RELATIVE. A clip starting at 00:05:00 has its first cue at 0:00:00.
That was a deliberate choice: see CLAUDE.md.

The one path with no media to cut is captions (YouTube always, Kaltura when the
entry has a caption track). Those arrive whole and free, so transcribe.sh
applies the same window to the segments instead, shifting them to the same
clip-relative timebase — `window_segments` here is that shared implementation,
so both halves can never disagree about what "00:05:00" means.

CLI:
  clip.py parse "00:05:00-01:30:00"      -> JSON on stdout
  clip.py cut SRC DEST "00:05:00-01:30:00"
  clip.py segments SPEC < segments.json  -> windowed segments.json on stdout

Environment:
  CLIP_REENCODE  default 0. See cut() — 0 stream-copies (seconds, but the cut
                 lands on the preceding keyframe), 1 re-encodes for a
                 frame-accurate start at the cost of a full transcode.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# HH:MM:SS(.mmm) / MM:SS(.mmm) / SS(.mmm). Bare seconds are allowed because
# "start at 90" is a reasonable thing to type for a short video, and there is
# no ambiguity: a colon form always has a colon in it.
_TIME_RE = re.compile(r"^(?:(?:(\d+):)?(\d+):)?(\d+(?:\.\d+)?)$")


class ClipError(ValueError):
    """A window that cannot be honoured. Always fatal, always before any spend."""


def parse_time(text):
    """"01:30:00" / "5:00" / "90" -> seconds as a float."""
    text = text.strip()
    if not text:
        raise ClipError("empty timestamp")
    m = _TIME_RE.match(text)
    if not m:
        raise ClipError(
            f"cannot read {text!r} as a timestamp — "
            "expected HH:MM:SS, MM:SS, or a number of seconds"
        )
    hours, minutes, seconds = m.group(1), m.group(2), m.group(3)
    total = float(seconds)
    if minutes is not None:
        total += int(minutes) * 60
    if hours is not None:
        total += int(hours) * 3600
    return total


def parse_clip(spec):
    """"00:05:00-01:30:00" -> (start_seconds, end_seconds or None).

    Open ends are allowed at both sides: "00:05:00-" runs to the end of the
    video, "-01:30:00" starts at zero. A bare "00:05:00" means the same as
    "00:05:00-" rather than being an error, because that is the only thing it
    could reasonably mean.

    The separator is "-", which never appears inside a timestamp, so no
    quoting subtleties: the split is unambiguous.
    """
    if spec is None:
        return None
    spec = spec.strip()
    if not spec:
        raise ClipError("empty --clip window")
    # Tolerate the en-dash a copy-paste from a document brings along, and
    # "00:05:00 to 01:30:00" spelled out.
    spec = spec.replace("–", "-").replace("—", "-")
    spec = re.sub(r"\s+to\s+", "-", spec, flags=re.IGNORECASE)
    parts = [p.strip() for p in spec.split("-")]
    if len(parts) == 1:
        parts.append("")
    if len(parts) != 2:
        raise ClipError(
            f"cannot read {spec!r} as a window — expected START-END, "
            "e.g. 00:05:00-01:30:00"
        )
    start = parse_time(parts[0]) if parts[0] else 0.0
    # "end" is how label() spells an open end, and the label is what goes into
    # state.json and gets parsed back by the clip stage on every attempt. If
    # this word were not accepted, `--clip 00:05:00-` would parse fine at the
    # command line and then fail on the round trip, one download later.
    # test_every_label_parses_back_to_its_own_window holds the invariant.
    if parts[1].lower() in ("", "end", "eof"):
        end = None
    else:
        end = parse_time(parts[1])
    if end is not None and end <= start:
        raise ClipError(
            f"the window ends at or before it starts ({parts[0] or '0'} -> {parts[1]})"
        )
    if start == 0.0 and end is None:
        raise ClipError("that window is the whole video — drop --clip instead")
    return (start, end)


def _hhmmss(seconds):
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}{m:02d}{s:02d}"


def label(window):
    """Human form for logs, the provenance comment, and error messages."""
    start, end = window
    def fmt(v):
        total = int(round(v))
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{fmt(start)}-{fmt(end)}" if end is not None else f"{fmt(start)}-end"


def token(window):
    """The run-id fragment: `c000500-013000`.

    A clipped run gets its own run id, so clipping a lecture twice — or
    clipping one already summarized in full — can never resume into or
    overwrite the other's artifacts. Every artifact path derives from the run
    id, so this one string is what keeps the .txt, .srt, .md and .pdf apart.
    """
    start, end = window
    return f"c{_hhmmss(start)}-" + (_hhmmss(end) if end is not None else "end")


def window_segments(segments, window):
    """Clip caption segments to the window and shift them to clip-relative time.

    Used only where there is no media to cut (YouTube captions, and a Kaltura
    entry that has a caption track). A segment that straddles a boundary is
    kept and truncated rather than dropped — the words were spoken inside the
    window, and losing the first sentence of a lecture because the caption cue
    began two seconds early is a worse answer than a slightly long cue.
    """
    start, end = window
    start_ms = int(round(start * 1000))
    end_ms = int(round(end * 1000)) if end is not None else None
    out = []
    for seg in segments:
        seg_start = int(seg.get("offset_ms", 0))
        seg_end = seg_start + max(int(seg.get("duration_ms", 0)), 1)
        if seg_end <= start_ms:
            continue
        if end_ms is not None and seg_start >= end_ms:
            continue
        new_start = max(seg_start, start_ms)
        new_end = min(seg_end, end_ms) if end_ms is not None else seg_end
        kept = dict(seg)
        kept["offset_ms"] = new_start - start_ms
        kept["duration_ms"] = max(new_end - new_start, 1)
        out.append(kept)
    return out


def _truthy(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def cut(src, dest, window, reencode=None):
    """Trim `src` to `window`, writing `dest`. Returns dest.

    `-ss` goes BEFORE `-i` so ffmpeg seeks rather than decoding and discarding
    everything up to the start — on a 90-minute lecture that is the difference
    between seconds and minutes.

    Stream copy is the default. It costs a couple of seconds instead of a full
    transcode (this box encodes at 2.3x realtime, so re-encoding an 85-minute
    window is over half an hour of CPU), and the price is that the cut lands on
    the keyframe at or before the requested start — up to one GOP early, a few
    seconds on the recorder's own output. For a summary window that is
    immaterial; set CLIP_REENCODE=1 when it isn't.

    `-avoid_negative_ts make_zero` is what makes the output start at t=0
    instead of carrying the source's timestamps forward. Without it the clip's
    first frame is still stamped 00:05:00, every SRT cue and frame timestamp
    inherits that offset, and the "relative" timebase this whole module
    promises silently becomes absolute.

    The write goes to a `.part` file and is renamed, for the same reason the
    Kaltura download does it: a half-written clip that looks like a finished
    artifact would be picked up by the resume logic and fail two stages later,
    inside ffmpeg or AssemblyAI, with nothing pointing back to here.

    Note the name: `clip.part.mp4`, NOT `clip.mp4.part`. ffmpeg chooses its
    muxer from the output file's extension, and a name ending in `.part` has
    none it recognises — it refuses to start with "Unable to choose an output
    format". The suffix has to survive on the temporary file too.
    """
    start, end = window
    if reencode is None:
        reencode = _truthy(os.environ.get("CLIP_REENCODE"))
    dest_path = Path(dest)
    part = str(dest_path.with_name(dest_path.stem + ".part" + dest_path.suffix))
    cmd = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error",
           "-ss", f"{start:.3f}"]
    if end is not None:
        # -t (duration), not -to: with -ss before -i the input timestamps have
        # already been rebased, so -to would be measured from the wrong origin.
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-i", str(src)]
    if reencode:
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-c:a", "aac", "-b:a", "128k"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-avoid_negative_ts", "make_zero", part]

    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise ClipError(
            f"ffmpeg could not cut {label(window)} out of {src}:\n"
            + (proc.stderr or "").strip()
        )
    if not os.path.exists(part) or os.path.getsize(part) == 0:
        try:
            os.unlink(part)
        except OSError:
            pass
        raise ClipError(
            f"cutting {label(window)} out of {src} produced an empty file — "
            "is the window past the end of the video?"
        )
    os.replace(part, str(dest))
    return dest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    cmd = argv[0]
    try:
        if cmd == "parse":
            if len(argv) != 2:
                print("usage: clip.py parse SPEC", file=sys.stderr)
                return 2
            window = parse_clip(argv[1])
            start, end = window
            print(json.dumps({
                "start": start,
                "end": end,
                "label": label(window),
                "token": token(window),
            }))
            return 0
        if cmd == "cut":
            if len(argv) != 4:
                print("usage: clip.py cut SRC DEST SPEC", file=sys.stderr)
                return 2
            cut(argv[1], argv[2], parse_clip(argv[3]))
            print(argv[2])
            return 0
        if cmd == "segments":
            if len(argv) != 2:
                print("usage: clip.py segments SPEC < segments.json", file=sys.stderr)
                return 2
            window = parse_clip(argv[1])
            segments = json.loads(sys.stdin.read())
            json.dump(window_segments(segments, window), sys.stdout)
            return 0
    except ClipError as exc:
        print(f"clip: {exc}", file=sys.stderr)
        return 1
    print(f"clip.py: unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
