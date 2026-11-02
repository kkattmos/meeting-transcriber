#!/usr/bin/env python3
"""
Extract frames from a meeting recording for the AI summary agent.

A frame is saved when the picture CHANGES, not on a clock. One ffmpeg decode
samples the video every FRAME_CHECK_SECONDS (2) and pipes each sample here at
full resolution; nothing touches the disk until a sample is kept. Each sample
is compared with the last saved frame by framecrop's texture hash over the
slide region — the same test llm_client's duplicate pass uses, so a moved
cursor or the participant filmstrip is not a change, a new title or new body
text is.

  * A changed picture is saved once it has SETTLED: the next sample shows the
    same thing. A slide mid-fade, or a bullet mid-animation, is never the
    frame that is kept.
  * A picture that never settles (a played video, a full-screen camera) still
    gets one frame per FRAME_MOTION_SECONDS (30), so it isn't invisible.
  * If nothing was saved for FRAME_SAFETY_SECONDS (300), one frame is saved
    anyway, in case the change was too small for the hash to see.
  * Blank frames (one flat colour: a share stopping, a fade through black)
    are never saved, and never count as a change.

This replaced a scene-change pass plus a fixed periodic pass (settled with the
operator 2026-09-30): two full decodes of the video, and a frame every minute
of a static slide that the duplicate pass then threw away.

Writes a manifest.json listing each frame's path, timestamp, and kind:
  "scene_change"  the picture changed and settled
  "motion"        the picture kept changing; the FRAME_MOTION_SECONDS sample
  "periodic"      the FRAME_SAFETY_SECONDS safety net

CLI:
  python3 screen/extract_frames.py <video_path> <output_dir> [<meeting_name>]

Environment:
  FRAME_CHECK_SECONDS    default 2    how often the picture is looked at
  FRAME_MOTION_SECONDS   default 30   one frame per this while it keeps moving
  FRAME_SAFETY_SECONDS   default 300  a frame after this long without one (0 = off)
  FRAME_CHANGE_DISTANCE  default 16   hash bits that make a new picture
  FRAME_DECODE_THREADS   default 1    ffmpeg decoder threads (0 = automatic)
  PDF_FRAME_CROP         slide        the region the hash is taken over

The output directory is always an argument — FRAMES_DIR from .env only decides
where the *caller* (summarize.py / lib/run_one.sh) puts the per-run directory.
"""
import json
import os
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "summarize"))

import framecrop  # noqa: E402

DEFAULT_CHECK_SECONDS = 2.0
DEFAULT_MOTION_SECONDS = 30.0
DEFAULT_SAFETY_SECONDS = 300.0
# One decoder thread: on the PC (Core 7 150U) ffmpeg's automatic threading
# spent ~2x the CPU for the same decode (86 vs 40 CPU-s on a 15-minute
# recording) to finish sooner. The operator's concern was CPU; 0 = automatic.
DEFAULT_DECODE_THREADS = 1
# Measured against ffmpeg's own JPEG default, which the frames used to be
# written with: quality 60 gives the same PSNR (36.4 dB on a real Meet frame)
# at about the same size; 85 was 50% larger for nothing the PDF can show.
JPEG_QUALITY = 60

RETIRED_VARS = ("SCENE_THRESHOLD", "FRAME_PERIOD_SECONDS")


class ChangeDetector:
    """Decides, sample by sample, which samples become saved frames.

    Pure bookkeeping over (timestamp, hash) so it can be tested without video.
    feed() answers with the kind to save the *current* sample as, or None;
    finish() answers for the last sample fed, when the video ended on a
    change that had no later sample to settle on. A hash of None is a blank
    sample: never saved, never a change.
    """

    def __init__(self, distance, motion_s, safety_s):
        self.distance = distance
        self.motion_s = motion_s
        self.safety_s = safety_s
        self.saved_hash = None
        self.saved_ts = None
        self.pending_hash = None   # the last sample that differed from the saved frame
        self.moving_since = None   # when the picture first left the saved frame

    def _same(self, a, b):
        return framecrop.hamming(a, b) <= self.distance

    def _saved(self, ts, digest, kind):
        self.saved_hash, self.saved_ts = digest, ts
        self.pending_hash = self.moving_since = None
        return kind

    def feed(self, ts, digest):
        if digest is None:
            return None
        if self.saved_hash is not None and self._same(digest, self.saved_hash):
            # Back on (or still on) the saved picture: any change was transient.
            self.pending_hash = self.moving_since = None
            if self.safety_s > 0 and ts - self.saved_ts >= self.safety_s:
                return self._saved(ts, digest, "periodic")
            return None
        if self.pending_hash is not None and self._same(digest, self.pending_hash):
            return self._saved(ts, digest, "scene_change")
        if self.moving_since is None:
            self.moving_since = ts
        elif ts - self.moving_since >= self.motion_s:
            return self._saved(ts, digest, "motion")
        self.pending_hash = digest
        return None

    def finish(self):
        return "scene_change" if self.pending_hash is not None else None


def _env_float(name, default, minimum):
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{name}={raw!r} is not a number")
    if value < minimum:
        raise SystemExit(f"{name}={raw!r} must be at least {minimum:g}")
    return value


def probe(video):
    """(duration seconds, has a video stream). (0.0, False) if unreadable."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type",
             "-of", "default=noprint_wrappers=1", video],
            text=True,
        )
    except (subprocess.CalledProcessError, OSError):
        return 0.0, False
    duration, has_video = 0.0, False
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if key == "codec_type" and value.strip() == "video":
            has_video = True
        elif key == "duration":
            try:
                duration = float(value)
            except ValueError:
                pass
    return duration, has_video


def _read_ppm(stream):
    """One binary PPM (P6, 8-bit) off ffmpeg's image2pipe: (w, h, rgb bytes).

    None at a clean end of stream. The header is four whitespace-separated
    tokens (P6, width, height, maxval) and exactly one whitespace byte before
    the pixels.
    """
    tokens, token = [], b""
    while len(tokens) < 4:
        ch = stream.read(1)
        if not ch:
            if tokens or token:
                raise RuntimeError("ffmpeg's frame stream ended inside a header")
            return None
        if ch.isspace():
            if token:
                tokens.append(token)
                token = b""
        else:
            token += ch
    if tokens[0] != b"P6" or tokens[3] != b"255":
        raise RuntimeError(f"unexpected frame header from ffmpeg: {tokens!r}")
    width, height = int(tokens[1]), int(tokens[2])
    # A buffered pipe's read(n) blocks until n bytes or EOF: one call, no copy.
    data = stream.read(width * height * 3)
    if len(data) != width * height * 3:
        raise RuntimeError("ffmpeg's frame stream ended inside a frame")
    return width, height, data


def sample_frames(video, check_s, threads=1):
    """Yield (timestamp, PIL RGB image) every check_s seconds, one decode.

    The fps filter picks the source frame nearest each tick, so sample i is
    the picture at i * check_s (to within one source frame).
    """
    Image = framecrop.Image
    rate = 1 / Fraction(str(check_s))
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-threads", str(threads), "-i", video, "-an", "-sn", "-dn",
        "-vf", f"fps={rate.numerator}/{rate.denominator}",
        "-pix_fmt", "rgb24", "-c:v", "ppm", "-f", "image2pipe", "-",
    ]
    # stderr goes to a file, not a pipe: a damaged recording can log an error
    # per frame, and an undrained pipe would stall ffmpeg mid-video.
    with tempfile.TemporaryFile() as errlog:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errlog)
        index = 0
        try:
            while True:
                frame = _read_ppm(proc.stdout)
                if frame is None:
                    break
                width, height, data = frame
                yield index * check_s, Image.frombuffer(
                    "RGB", (width, height), data, "raw", "RGB", 0, 1)
                index += 1
        finally:
            proc.stdout.close()
            rc = proc.wait()
        errlog.seek(0)
        err = errlog.read().decode(errors="replace").strip()
    if rc != 0 and index == 0:
        raise RuntimeError(f"ffmpeg could not decode {video}: {err[-2000:]}")
    if err:
        tail = err.splitlines()[-1]
        print(f"    note: ffmpeg reported errors (last: {tail})", file=sys.stderr)


def extract(video, out_dir, check_s, motion_s, safety_s, distance, crop_mode,
            threads=1):
    """Run the detector over the video. Returns [(timestamp, kind, path)]."""
    detector = ChangeDetector(distance, motion_s, safety_s)
    kept = []
    last = None  # (ts, image) of the latest non-blank sample, for finish()
    samples = blanks = 0

    def save(ts, image, kind):
        path = out_dir / f"frame_{len(kept) + 1:05d}.jpg"
        image.save(path, "JPEG", quality=JPEG_QUALITY, optimize=True)
        kept.append((ts, kind, str(path)))

    for ts, image in sample_frames(video, check_s, threads):
        samples += 1
        gray = image.convert("L")
        if framecrop.is_blank(gray):
            blanks += 1
            digest = None
        else:
            digest = framecrop.frame_hash(gray, crop_mode=crop_mode)
            last = (ts, image)
        kind = detector.feed(ts, digest)
        if kind:
            save(ts, image, kind)
    kind = detector.finish()
    if kind and last:
        save(last[0], last[1], kind)
    print(f"    looked at {samples} samples ({blanks} blank)")
    return kept


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <video_path> <output_dir> [meeting_name]")
        sys.exit(1)

    video = sys.argv[1]
    out_dir = Path(sys.argv[2])
    meeting_name = sys.argv[3] if len(sys.argv) > 3 else "meeting"

    if framecrop.Image is None:
        raise SystemExit("extract_frames.py needs Pillow (it is in requirements.txt; "
                         "re-run setup.sh or `.venv/bin/pip install pillow`)")
    check_s = _env_float("FRAME_CHECK_SECONDS", DEFAULT_CHECK_SECONDS, 0.1)
    motion_s = _env_float("FRAME_MOTION_SECONDS", DEFAULT_MOTION_SECONDS, check_s)
    safety_s = _env_float("FRAME_SAFETY_SECONDS", DEFAULT_SAFETY_SECONDS, 0)
    distance = int(_env_float("FRAME_CHANGE_DISTANCE",
                              framecrop.SAME_SLIDE_MAX_DISTANCE, 0))
    crop_mode = framecrop.crop_mode_from_env()
    threads = int(_env_float("FRAME_DECODE_THREADS", DEFAULT_DECODE_THREADS, 0))
    for name in RETIRED_VARS:
        if os.environ.get(name, "").strip():
            print(f"    note: {name} is no longer used (frames are taken on change; "
                  f"see FRAME_CHECK_SECONDS and FRAME_SAFETY_SECONDS)")

    if out_dir.exists() and any(out_dir.iterdir()):
        print(f"WARNING: output dir {out_dir} already has files - reusing.")
    out_dir.mkdir(parents=True, exist_ok=True)

    print("==> Probing video")
    duration, has_video = probe(video)
    print(f"    duration = {duration:.1f}s")

    if has_video:
        print(f"==> Change detection (every {check_s:g}s, distance {distance}, "
              f"motion {motion_s:g}s, safety {safety_s:g}s)")
        kept = extract(video, out_dir, check_s, motion_s, safety_s, distance,
                       crop_mode, threads)
    else:
        print("    no video stream - no frames")
        kept = []
    counts = {k: sum(1 for f in kept if f[1] == k)
              for k in ("scene_change", "motion", "periodic")}
    print("    saved " + ", ".join(f"{n} {k}" for k, n in counts.items()))

    manifest = {
        "video": str(video),
        "meeting_name": meeting_name,
        "duration_seconds": duration,
        "check_seconds": check_s,
        "change_distance": distance,
        "motion_seconds": motion_s,
        "safety_seconds": safety_s if safety_s > 0 else None,
        "frame_count": len(kept),
        "frames": [
            {"timestamp_s": round(ts, 3), "kind": kind, "path": path}
            for (ts, kind, path) in kept
        ],
    }

    manifest_path = out_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"==> Wrote manifest with {len(kept)} frames -> {manifest_path}")


if __name__ == "__main__":
    main()
